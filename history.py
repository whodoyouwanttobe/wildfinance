"""
history.py — история продаж по артикулам и XYZ-анализ (матрица ABC × XYZ).

Как это работает
────────────────
1. После каждого отчёта бот сохраняет продажи по ДНЯМ и артикулам
   (таблица sales_daily) и запоминает, какой период покрывал отчёт
   (таблица report_ranges).
   • Повторная загрузка того же отчёта ничего не задваивает: перед записью
     удаляются старые данные за те же даты.
   • Отчёты могут быть любой длины (неделя WB, месяц Ozon) и пересекаться.
2. XYZ считается по ПОЛНЫМ календарным неделям (пн–вс), которые целиком
   покрыты загруженными отчётами. Нужно минимум MIN_WEEKS недель.
3. X/Y/Z — по коэффициенту вариации недельных продаж в штуках
   (стандартное отклонение / среднее). ABC — по сумме к выплате за те же недели.

Что настраивать после проверки на реальных отчётах
──────────────────────────────────────────────────
• MIN_WEEKS / MAX_WEEKS — сколько недель нужно и сколько берём в расчёт.
• X_MAX / Y_MAX — пороги вариации. Классические 10% / 25% придуманы для
  оптовых складов; продажи на маркетплейсах скачут сильнее, поэтому
  по умолчанию 25% / 50%.
• MAX_REPORT_DAYS — строки с датой старше (последняя дата − MAX_REPORT_DAYS)
  считаем «хвостами» (корректировки задним числом) и прижимаем к началу
  периода, чтобы один старый штраф не стёр месяцы истории.
• WEEKLY_SNAP_DAYS — отчёты короче считаются недельными: период выравнивается
  на календарную неделю (пн–вс), в которой больше всего строк.
• Колонки дат — DATE_COLUMNS в wb_parser.py и ozon_parser.py.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pandas as pd

import database
from abc_analysis import abc_classify, _label
from profit import article_label

MIN_WEEKS = 4
MAX_WEEKS = 8
X_MAX = 0.25
Y_MAX = 0.50
MAX_REPORT_DAYS = 35
# Отчёт короче этого числа дней считаем еженедельным и выравниваем на пн–вс
WEEKLY_SNAP_DAYS = 11
MAX_LISTED = 5

XYZ_HINTS = {
    "X": "стабильный спрос",
    "Y": "спрос колеблется",
    "Z": "спрос непредсказуем",
}

# Что делать с товаром в каждой клетке матрицы
CELL_ADVICE = {
    "AX": "главные деньги и ровный спрос — держите запас всегда, не допускайте out-of-stock",
    "AY": "много денег, но продажи скачут — запас с подушкой, следите за акциями и сезоном",
    "AZ": "много денег при хаотичном спросе — контролируйте вручную, ищите причину скачков",
    "BX": "стабильные середняки — можно растить рекламой и ценой",
    "BY": "середняки с колебаниями — поставки чаще и мельче",
    "BZ": "середняки с хаотичным спросом — не держите большой запас",
    "CX": "мало денег, но стабильно — проверьте цену и маржу, возможно, недооценены",
    "CY": "мало денег и нестабильно — минимальный запас",
    "CZ": "мало денег и хаотичный спрос — первые кандидаты на вывод",
}


# ─── Хранение ───────────────────────────────────────────────────────────────

def parse_dates(series: pd.Series) -> pd.Series:
    """Даты WB/Ozon: 2025-09-13, 13.09.2025, 2025-09-13 10:15:00, Excel-даты."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.normalize()
    s = series.astype(str).str.strip()
    s = s.where(~s.isin({"", "nan", "NaT", "None"}))
    iso = s.str.match(r"^\d{4}-\d{2}-\d{2}", na=False)
    out = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    if iso.any():
        out[iso] = pd.to_datetime(s[iso].str[:10], format="%Y-%m-%d", errors="coerce")
    rest = s.notna() & ~iso
    if rest.any():
        out[rest] = pd.to_datetime(s[rest].str[:10], format="%d.%m.%Y", errors="coerce")
    return out.dt.normalize()


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def daily_from_metrics(metrics: dict, today: date | None = None) -> tuple[pd.DataFrame, date, date, bool]:
    """
    Построчные данные отчёта → продажи по дням и артикулам.
    Возвращает (таблица, начало периода, конец периода, нашлись_ли_даты).
    Если дат нет — считаем отчёт за прошлую неделю (так выгружает WB).
    """
    rows = metrics.get("rows")
    if rows is None or rows.empty:
        raise ValueError("В отчёте нет построчных данных")
    rows = rows.copy()
    rows["артикул"] = rows["артикул"].fillna("").astype(str).str.strip()
    rows = rows[rows["артикул"] != ""]

    dates = parse_dates(rows["дата"]) if "дата" in rows else pd.Series(pd.NaT, index=rows.index)
    dated = dates.notna().any()
    if dated:
        valid = dates.dropna()
        end = valid.max().date()
        start = max(valid.min().date(), end - timedelta(days=MAX_REPORT_DAYS))
        if (end - start).days < WEEKLY_SNAP_DAYS:
            # Еженедельный отчёт: период = неделя (пн–вс), в которой больше всего строк.
            # Даты продаж часто не покрывают все дни (нет продаж в понедельник
            # или есть хвост с прошлой недели) — поэтому «прилипаем» к неделе.
            week = valid.dt.date.map(_week_start).value_counts().idxmax()
            start, end = week, week + timedelta(days=6)
        filled = dates.fillna(pd.Timestamp(end))
        clipped = filled.clip(lower=pd.Timestamp(start), upper=pd.Timestamp(end))
    else:
        today = today or date.today()
        start = _week_start(today) - timedelta(days=7)
        end = start + timedelta(days=6)
        clipped = pd.Series(pd.Timestamp(start), index=rows.index)
    rows["день"] = clipped.dt.strftime("%Y-%m-%d")

    grouped = metrics.get("grouped")
    labels, names = {}, {}
    if grouped is not None:
        for key, row in grouped.iterrows():
            labels[str(key)] = article_label(key, row)
            names[str(key)] = str(row.get("название", "") or "").strip()

    daily = rows.groupby(["день", "артикул"], as_index=False).agg(
        шт=("шт", "sum"), выплата=("выплата", "sum"), выручка=("выручка", "sum"),
    )
    daily["метка"] = daily["артикул"].map(lambda k: labels.get(k) or _label(k))
    daily["название"] = daily["артикул"].map(lambda k: names.get(k, ""))
    return daily, start, end, bool(dated)


def save_report(user_id: int, metrics: dict, db_path: str | None = None) -> dict:
    """Сохраняет отчёт в историю. Возвращает статус (см. status())."""
    db_path = db_path or database.DB_PATH
    mp = metrics.get("marketplace", "WB")
    daily, start, end, dated = daily_from_metrics(metrics)
    now = datetime.utcnow().isoformat()
    conn = database.get_connection(db_path)
    try:
        conn.execute(
            "DELETE FROM sales_daily WHERE user_id = ? AND marketplace = ? AND day BETWEEN ? AND ?",
            (user_id, mp, start.isoformat(), end.isoformat()),
        )
        conn.executemany(
            "INSERT INTO sales_daily (user_id, marketplace, day, article, label, name, qty, payout, revenue) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (user_id, mp, r.день, r.артикул, r.метка, r.название,
                 float(r.шт), float(r.выплата), float(r.выручка))
                for r in daily.itertuples(index=False)
            ],
        )
        conn.execute(
            "INSERT INTO report_ranges (user_id, marketplace, start_day, end_day, dated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, mp, start.isoformat(), end.isoformat(), int(dated), now),
        )
        conn.commit()
    finally:
        conn.close()
    st = status(user_id, mp, db_path=db_path)
    st.update(start=start, end=end, dated=dated)
    return st


def _load(user_id: int, mp: str, db_path: str) -> tuple[pd.DataFrame, list[tuple[date, date]]]:
    conn = database.get_connection(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT day, article, label, name, qty, payout, revenue FROM sales_daily "
            "WHERE user_id = ? AND marketplace = ?",
            conn, params=(user_id, mp),
        )
        ranges = [
            (date.fromisoformat(a), date.fromisoformat(b))
            for a, b in conn.execute(
                "SELECT start_day, end_day FROM report_ranges WHERE user_id = ? AND marketplace = ?",
                (user_id, mp),
            ).fetchall()
        ]
    finally:
        conn.close()
    return df, ranges


def complete_weeks(ranges: list[tuple[date, date]]) -> list[date]:
    """Понедельники недель, все 7 дней которых покрыты отчётами (по возрастанию)."""
    covered: set[date] = set()
    for a, b in ranges:
        d = a
        while d <= b:
            covered.add(d)
            d += timedelta(days=1)
    weeks = sorted({_week_start(d) for d in covered})
    return [w for w in weeks if all(w + timedelta(days=i) in covered for i in range(7))]


def status(user_id: int, mp: str, db_path: str | None = None) -> dict:
    db_path = db_path or database.DB_PATH
    _df, ranges = _load(user_id, mp, db_path)
    weeks = complete_weeks(ranges)
    return {"marketplace": mp, "weeks": len(weeks), "ready": len(weeks) >= MIN_WEEKS,
            "last_week": weeks[-1] if weeks else None}


def marketplaces_with_history(user_id: int, db_path: str | None = None) -> list[str]:
    conn = database.get_connection(db_path or database.DB_PATH)
    try:
        rows = conn.execute(
            "SELECT DISTINCT marketplace FROM report_ranges WHERE user_id = ? ORDER BY marketplace DESC",
            (user_id,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


# ─── XYZ ────────────────────────────────────────────────────────────────────

def xyz_class(cv: float) -> str:
    if cv <= X_MAX:
        return "X"
    if cv <= Y_MAX:
        return "Y"
    return "Z"


def compute_matrix(user_id: int, mp: str, db_path: str | None = None) -> dict:
    """
    Матрица ABC × XYZ по последним полным неделям.
    Возвращает {"ready": bool, "weeks": [...], "table": DataFrame | None}.
    table: index = артикул; столбцы метка, шт_всего, выплата, cv, abc, xyz, недели (список шт).
    """
    db_path = db_path or database.DB_PATH
    df, ranges = _load(user_id, mp, db_path)
    weeks = complete_weeks(ranges)[-MAX_WEEKS:]
    if len(weeks) < MIN_WEEKS or df.empty:
        return {"ready": False, "weeks": weeks, "table": None, "marketplace": mp}

    df["week"] = pd.to_datetime(df["day"]).dt.date.map(_week_start)
    df = df[df["week"].isin(weeks)]
    qty = df.pivot_table(index="article", columns="week", values="qty", aggfunc="sum", fill_value=0.0)
    qty = qty.reindex(columns=weeks, fill_value=0.0).clip(lower=0)
    payout = df.groupby("article")["payout"].sum()
    labels = df.groupby("article")["label"].last()

    mean = qty.mean(axis=1)
    std = qty.std(axis=1, ddof=0)
    cv = (std / mean).where(mean > 0, math.inf)

    table = pd.DataFrame({
        "метка": labels.reindex(qty.index).fillna(pd.Series(qty.index, index=qty.index)),
        "шт_всего": qty.sum(axis=1),
        "выплата": payout.reindex(qty.index).fillna(0.0),
        "cv": cv,
    })
    table["xyz"] = table["cv"].map(lambda v: xyz_class(v) if math.isfinite(v) else "—")
    abc = abc_classify(table["выплата"])
    table["abc"] = abc["класс"].reindex(table.index)
    table["недели"] = [list(map(float, qty.loc[a].tolist())) for a in table.index]
    return {"ready": True, "weeks": weeks, "table": table, "marketplace": mp}


def _names(part: pd.DataFrame) -> str:
    import html
    part = part.sort_values("выплата", ascending=False)
    items = [html.escape(str(x)) for x in part["метка"].head(MAX_LISTED)]
    rest = len(part) - len(items)
    return ", ".join(items) + (f" и ещё {rest}" if rest > 0 else "")


def render(result: dict) -> str:
    """HTML-блок для Telegram."""
    mp = result["marketplace"]
    weeks = result["weeks"]
    if not result["ready"]:
        have = len(weeks)
        return (
            f"📈 <b>ABC × XYZ ({mp})</b>\n\n"
            f"Для XYZ нужна история минимум за {MIN_WEEKS} полные недели (пн–вс). "
            f"Сейчас накоплено: <b>{have} из {MIN_WEEKS}</b>.\n\n"
            "Пришли отчёты за прошлые недели — можно все сразу, по одному файлу. "
            "Повторная загрузка того же отчёта ничего не задвоит."
        )

    t = result["table"]
    first, last = weeks[0], weeks[-1] + timedelta(days=6)
    lines = [
        f"📈 <b>ABC × XYZ — {mp}</b>",
        f"<i>{len(weeks)} нед.: {first:%d.%m} – {last:%d.%m.%Y}. "
        "ABC — по сумме к выплате, XYZ — по стабильности продаж в штуках.</i>",
        "",
    ]
    grid = t[t["abc"].isin(["A", "B", "C"]) & t["xyz"].isin(["X", "Y", "Z"])]
    header = "     X    Y    Z"
    rows = [header]
    for a in "ABC":
        cells = [len(grid[(grid["abc"] == a) & (grid["xyz"] == x)]) for x in "XYZ"]
        rows.append(f"{a}  " + "".join(f"{c:>5}" for c in cells))
    lines.append("<pre>" + "\n".join(rows) + "</pre>")
    lines.append(
        f"X — вариация ≤ {X_MAX:.0%} ({XYZ_HINTS['X']}), Y — до {Y_MAX:.0%}, Z — больше ({XYZ_HINTS['Z']})."
    )

    for cell, advice in CELL_ADVICE.items():
        part = grid[(grid["abc"] == cell[0]) & (grid["xyz"] == cell[1])]
        if part.empty:
            continue
        icon = {"A": "🟢", "B": "🟡", "C": "⚪️"}[cell[0]]
        lines.append(f"\n{icon} <b>{cell}</b> — {len(part)} шт.: {_names(part)}")
        lines.append(f"   💡 <i>{advice}</i>")

    loss = t[t["abc"] == "loss"]
    if not loss.empty:
        lines.append(f"\n🔴 <b>Убыточные за период</b> — {len(loss)} шт.: {_names(loss)}")
        lines.append("   💡 <i>в матрицу не входят — сначала исправьте юнит-экономику</i>")
    no_sales = t[(t["xyz"] == "—") & (t["abc"] != "loss")]
    if not no_sales.empty:
        lines.append(f"\n💤 <b>Без продаж за период</b> — {len(no_sales)} шт.: {_names(no_sales)}")
    return "\n".join(lines)
