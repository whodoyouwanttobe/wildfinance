"""
ozon_parser.py — Анализ отчёта «Детализация начислений» Ozon.

Поддерживает формат из раздела seller.ozon.ru/app/finances/accruals
(вкладка "Детализация начислений"): одна строка = одна услуга.

Архитектурно повторяет wb_parser.py, чтобы вызов из bot.py
был идентичен: analyze(filepath) -> str (HTML-отчёт).

Ozon-специфичные боли (отличаются от WB):
  1. «Покатушки» — отменённые отправления с удержаниями обратной
     логистики при отсутствии продажи.
  2. Высокий эквайринг (>2% от выручки — норма рынка 1.2–1.7%).
  3. Drop-off-обработка съедает маржу на тяжёлых товарах.
  4. Услуги партнёров (продвижение) > 15% выручки.
"""

import sys
import os
import pandas as pd

from abc_analysis import render_abc
from table_reader import read_table, to_num


# ─── Маппинг столбцов (Ozon отчёт по начислениям) ────────────────────────────

COLUMNS = {
    "id":         "ID начисления",
    "группа":     "Группа услуг",
    "тип":        "Тип начисления",
    "сумма":      "Сумма итого, руб",
    "дата":       "Дата начисления",
}

OPTIONAL = {
    "sku":            "SKU",
    "артикул":        "Артикул",
    "цена_продавца":  "Цена продавца",
    "схема":          "Схема работы",
    "тип_документа":  "Тип документа",
}

# Категории услуг
LOGISTICS_TYPES = (
    "обработка отправления", "drop-off", "обратная логистика",
    "последняя миля", "магистральная логистика", "доставка",
)
STORAGE_TYPES = ("хранение", "утилизация", "размещение")
PENALTY_TYPES = (
    "штраф", "неустойка", "удержание", "потеря", "порча",
    "подмена", "пересорт",
)
ACQUIRING_TYPES = ("эквайринг", "комиссия за эквайринг")
PARTNER_TYPES = (
    "продвижение", "реклама", "трафарет", "отзывы",
    "услуги партнёров", "баллы за скидки",
)

# Пороги «болей» (используются в generate_insight_ozon)
ACQUIRING_THRESHOLD_PCT = 2.0
PARTNER_THRESHOLD_PCT = 15.0
POKATUSHKI_MIN_TRIPS = 2

# ─── Утилиты ──────────────────────────────────────────────────────────────────


# Дата начисления для истории продаж (XYZ-анализ), по приоритету
DATE_COLUMNS = ("Дата начисления", "Дата операции", "Дата")


def detect_column_mapping(df: pd.DataFrame) -> dict:
    """Проверяет, что отчёт похож на Ozon (по обязательным колонкам)."""
    cols = set(df.columns)
    missing = [c for c in COLUMNS.values() if c not in cols]
    if missing:
        raise ValueError(
            "Похоже, это не отчёт Ozon «Детализация начислений».\n"
            f"Не найдены обязательные столбцы: {', '.join(missing)}\n"
            "Открой seller.ozon.ru → Финансы → Детализация начислений "
            "и выгрузи отчёт за нужный период."
        )
    mapping = dict(COLUMNS)
    for key, col in OPTIONAL.items():
        if col in cols:
            mapping[key] = col
    return mapping


def safe_str_col(df: pd.DataFrame, col_name: str) -> pd.Series:
    if col_name and col_name in df.columns:
        return df[col_name].fillna("").astype(str)
    return pd.Series("", index=df.index)


def safe_num_col(df: pd.DataFrame, col_name: str) -> pd.Series:
    if col_name and col_name in df.columns:
        return to_num(df[col_name])
    return pd.Series(0.0, index=df.index)


def load_report(filepath: str) -> pd.DataFrame:
    """Читает CSV (в т.ч. с «;») или Excel; сам находит строку заголовков."""
    return read_table(filepath)


def fmt(value: float) -> str:
    return f"{value:,.2f}"


def bold(text: str) -> str:
    return f"<b>{text}</b>"


def fmt_expense(value: float) -> str:
    if value > 0:
        return bold(f"-{fmt(value)}")
    return fmt(value)


def fmt_signed(value: float) -> str:
    """Сумма Ozon со знаком: списание «−» жирным, начисление «+»."""
    if value < -0.005:
        return bold(fmt(value))
    if value > 0.005:
        return f"+{fmt(value)}"
    return fmt(0.0)


def fmt_profit(value: float) -> str:
    if value < 0:
        return bold(fmt(value))
    return fmt(value)


def categorize(group: str, type_: str) -> str:
    """Возвращает: доход / логистика / хранение / штрафы / эквайринг / партнёры / прочее."""
    g = (group or "").lower().strip()
    t = (type_ or "").lower().strip()
    if "вознаграждение" in t or "комиссия за продажу" in t:
        return "комиссия"
    if "компенсац" in t or ("возмещение" in t and "возврат" not in t):
        return "компенсации"  # Ozon возмещает продавцу утерю/порчу — это «+»
    if any(s in t for s in LOGISTICS_TYPES) and "возврат" in t:
        return "логистика"
    if ("возврат" in t or g.startswith("возврат")) and "логист" not in t:
        return "доход"  # возврат выручки — отрицательная сумма, уменьшает доход
    if "продажа" in t or "выручка" in t or "возмещение" in t or g == "продажи":
        return "доход"
    if any(s in t for s in PENALTY_TYPES):
        return "штрафы"
    if any(s in t for s in ACQUIRING_TYPES):
        return "эквайринг"
    if any(s in t for s in PARTNER_TYPES) or "продвижение" in g or "партн" in g:
        return "партнёры"
    if any(s in t for s in STORAGE_TYPES):
        return "хранение"
    if any(s in t for s in LOGISTICS_TYPES) or "доставка" in g or "логистика" in g:
        return "логистика"
    return "прочее"


# ─── Ozon-советник ────────────────────────────────────────────────────────────


def generate_insight_ozon(sku: str, row, total_income: float) -> str:
    """
    Ozon-вариант «Советника». Боли:
      1. Покатушки (возвраты с обратной логистикой ≥ 2 шт. без продажи)
      2. Эквайринг > 2% от выручки по этому SKU
      3. Drop-off съедает маржу (>25% от выручки)
      4. Услуги партнёров > 15% от выручки
    """
    pokatushki = int(row.get("покатушки", 0))
    # В отчёте Ozon расходы со знаком «−» — берём модуль
    acquiring = abs(float(row.get("эквайринг", 0)))
    partner = abs(float(row.get("партнёры", 0)))
    drop_off = abs(float(row.get("дроп_офф", 0)))
    income = float(row.get("доход", 0))
    net = float(row.get("чистая_прибыль", 0))

    lines = []

    if pokatushki >= POKATUSHKI_MIN_TRIPS and income <= 0:
        return (
            f"🚨 <b>Обнаружена аномалия!</b> "
            f"{pokatushki} отправлений с обратной логистикой, но без продаж.\n"
            f"     Проверьте регион доставки — возможна атака конкурентов "
            f"или фейковые заказы."
        )

    if income > 0:
        acq_pct = acquiring / income * 100
        if acq_pct > ACQUIRING_THRESHOLD_PCT:
            lines.append(
                f"💳 Эквайринг {fmt(acq_pct)}% от выручки (норма 1.2–1.7%).\n"
                f"     Проверьте, не указан ли высокий тариф в личном кабинете."
            )
        if partner > 0 and partner / income * 100 > PARTNER_THRESHOLD_PCT:
            lines.append(
                f"📣 Услуги партнёров (продвижение) съедают "
                f"{fmt(partner / income * 100)}% выручки.\n"
                f"     Снизьте ставку или отключите неэффективные кампании."
            )

    if drop_off > 0 and income > 0 and drop_off / income > 0.25:
        lines.append(
            f"📦 Drop-off-обработка слишком дорогая "
            f"({fmt(drop_off / income * 100)}% от выручки).\n"
            f"     Для тяжёлых товаров выгоднее отправлять со своего склада (FBO)."
        )

    if net < 0 and not lines:
        lines.append("💡 Пересмотрите цену или закупочную стоимость.")

    return "\n".join(lines) if lines else ""


# ─── Основная аналитика ───────────────────────────────────────────────────────


def _group_ozon(df: pd.DataFrame) -> pd.DataFrame:
    """Группировка отчёта Ozon по SKU с разбивкой по категориям расходов.

    Используем pivot вместо groupby+lambda, чтобы избежать проблем
    с несовпадением индексов в groupby+agg(lambda с df.loc).
    """
    sku_col = "SKU" if "SKU" in df.columns else None
    art_col = "Артикул" if "Артикул" in df.columns else None

    def _sku_key(row):
        if sku_col:
            v = row[sku_col]
            if pd.notna(v) and str(v).strip():
                return f"SKU:{v}"
        if art_col:
            v = row[art_col]
            if pd.notna(v) and str(v).strip():
                return f"ART:{v}"
        return f"ID:{row['_id']}"

    df["_sku"] = df.apply(_sku_key, axis=1)

    # Покатушки: ID-начисления без продажи, но с обратной логистикой
    has_sale = (df["_категория"] == "доход").groupby(df["_id"]).transform("any")
    has_reverse = df["_тип"].str.lower().str.contains("обратная логистика", na=False)
    df["_покатушка"] = (~has_sale & has_reverse).astype(int)

    # Drop-off как отдельная метрика
    df["_дроп_офф"] = df["_сумма"].where(
        df["_тип"].str.lower().str.contains("drop-off", na=False), 0
    )

    # Pivot: SKU × категория → сумма. Чисто и без проблем с индексами.
    pivot = (
        df.pivot_table(
            index="_sku",
            columns="_категория",
            values="_сумма",
            aggfunc="sum",
            fill_value=0,
        )
        .rename_axis(None, axis=1)
    )
    # Гарантируем, что все категории присутствуют (даже если 0)
    for cat in ("доход", "комиссия", "логистика", "хранение", "штрафы", "эквайринг", "партнёры", "прочее", "компенсации"):
        if cat not in pivot.columns:
            pivot[cat] = 0.0

    # Проданные штуки: строки выручки «+» минус возвраты выручки «−»
    t = df["_тип"].str.lower()
    is_income = df["_категория"] == "доход"
    sale_row = is_income & (df["_сумма"] > 0) & ~t.str.contains("возмещение|компенсац", na=False)
    return_row = is_income & (df["_сумма"] < 0)
    qty_col = next((c for c in ("Количество", "Кол-во", "Количество, шт") if c in df.columns), None)
    qty = to_num(df[qty_col]).abs().where(lambda x: x > 0, 1.0) if qty_col else pd.Series(1.0, index=df.index)
    df["_шт"] = qty.where(sale_row, 0) - qty.where(return_row, 0)
    name_col = next((c for c in ("Название товара", "Наименование товара", "Название", "Товар") if c in df.columns), None)
    df["_название"] = df[name_col].fillna("").astype(str).str.strip() if name_col else ""
    df["_артикул"] = (
        df[art_col].fillna("").astype(str).str.strip() if art_col else pd.Series("", index=df.index)
    )

    # Дополнительные агрегаты
    extra = df.groupby("_sku").agg(
        покатушки=("_покатушка", "sum"),
        дроп_офф=("_дроп_офф", "sum"),
        строк=("_сумма", "count"),
        шт=("_шт", "sum"),
        название=("_название", lambda x: next((v for v in x if v and v != "nan"), "")),
        артикул=("_артикул", lambda x: next((v for v in x if v and v != "nan"), "")),
    )

    grouped = pivot.join(extra, how="outer")
    for c in grouped.columns:
        if c not in ("название", "артикул"):
            grouped[c] = grouped[c].fillna(0)
    grouped[["название", "артикул"]] = grouped[["название", "артикул"]].fillna("")
    grouped["выручка_брутто"] = grouped["доход"]
    # Суммы в отчёте Ozon уже со знаком: начисления «+», списания «−».
    # Чистая прибыль (к перечислению) = сумма всех строк. Возмещения и
    # компенсации со знаком «+» уменьшают расходы, а не увеличивают их.
    money_cols = ["доход", "комиссия", "логистика", "хранение", "штрафы", "эквайринг", "партнёры", "прочее", "компенсации"]
    grouped["чистая_прибыль"] = grouped[money_cols].sum(axis=1)
    return grouped


def compute(filepath: str) -> dict:
    """
    Считает метрики Ozon-отчёта без форматирования (для analyze и /compare).
    Шаги:
      1) Загрузить файл, проверить формат.
      2) Категоризировать каждую строку.
      3) Сгруппировать по SKU.
      4) Посчитать итоги.
    """
    df = load_report(filepath)
    mapping = detect_column_mapping(df)

    df["_сумма"] = safe_num_col(df, mapping["сумма"])
    df["_группа"] = safe_str_col(df, mapping["группа"])
    df["_тип"] = safe_str_col(df, mapping["тип"])
    df["_id"] = safe_str_col(df, mapping["id"])

    df["_категория"] = [
        categorize(g, t)
        for g, t in zip(df["_группа"], df["_тип"])
    ]

    grouped = _group_ozon(df)
    date_col = next((c for c in DATE_COLUMNS if c in df.columns), None)
    rows = pd.DataFrame({
        "дата": df[date_col] if date_col else pd.Series(None, index=df.index, dtype=object),
        "артикул": df["_sku"],
        "шт": df["_шт"],
        "выплата": df["_сумма"],
        "выручка": df["_сумма"].where(df["_категория"] == "доход", 0),
    })
    return {
        "marketplace": "Ozon",
        "rows": rows,
        "доход": float(grouped["доход"].sum()),
        "логистика": float(grouped["логистика"].sum()),
        "хранение": float(grouped["хранение"].sum()),
        "штрафы": float(grouped["штрафы"].sum()),
        "эквайринг": float(grouped["эквайринг"].sum()),
        "партнёры": float(grouped["партнёры"].sum()),
        "прочее": float(grouped["прочее"].sum()),
        "компенсации": float(grouped["компенсации"].sum()),
        "комиссия": float(grouped["комиссия"].sum()),
        "чистая_прибыль": float(grouped["чистая_прибыль"].sum()),
        "grouped": grouped,
    }


def analyze(filepath: str, m: dict | None = None) -> str:
    """HTML-отчёт по Ozon-отчёту (метрики считает compute())."""
    grouped = (m or compute(filepath))["grouped"]

    total_income = grouped["доход"].sum()
    total_logistics = grouped["логистика"].sum()
    total_storage = grouped["хранение"].sum()
    total_penalties = grouped["штрафы"].sum()
    total_acquiring = grouped["эквайринг"].sum()
    total_partner = grouped["партнёры"].sum()
    total_other = grouped["прочее"].sum()
    total_drop_off = grouped["дроп_офф"].sum()
    total_profit = grouped["чистая_прибыль"].sum()
    total_pokatushki = int(grouped["покатушки"].sum())

    lines = []
    lines.append("📦 <b>ОТЧЁТ OZON</b>")
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
    lines.append("🏆 <b>ОБЩИЙ ФИНАНСОВЫЙ ИТОГ</b>")
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
    lines.append(f"💰 Выручка (продажи − возвраты): {fmt_signed(total_income)} руб.")
    total_commission = grouped["комиссия"].sum()
    if abs(total_commission) > 0.005:
        lines.append(f"🏷 Вознаграждение Ozon:     {fmt_signed(total_commission)} руб.")
    lines.append(f"🚚 Логистика (вся):         {fmt_signed(total_logistics)} руб.")
    if abs(total_drop_off) > 0.005:
        lines.append(f"    └ 📦 в т.ч. Drop-off:  {fmt_signed(total_drop_off)} руб.")
    lines.append(f"📦 Хранение + утилизация:   {fmt_signed(total_storage)} руб.")
    lines.append(f"🔴 Штрафы и удержания:      {fmt_signed(total_penalties)} руб.")
    if abs(total_acquiring) > 0.005:
        lines.append(f"💳 Эквайринг:                {fmt_signed(total_acquiring)} руб.")
    if abs(total_partner) > 0.005:
        lines.append(f"📣 Услуги партнёров:         {fmt_signed(total_partner)} руб.")
    if abs(total_other) > 0.005:
        lines.append(f"🧮 Прочие начисления:        {fmt_signed(total_other)} руб.")
    total_comp = grouped["компенсации"].sum()
    if abs(total_comp) > 0.005:
        lines.append(f"💚 Компенсации от Ozon:       {fmt_signed(total_comp)} руб.")
    lines.append("")
    profit_icon = "✅" if total_profit >= 0 else "🚨"
    lines.append(
        f"{profit_icon} <b>К ВЫПЛАТЕ ОТ OZON: {fmt_profit(total_profit)} руб.</b>\n"
        "<i>до себестоимости и налогов</i>"
    )

    if total_pokatushki >= POKATUSHKI_MIN_TRIPS:
        lines.append("")
        lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
        lines.append("🚨 <b>ОБНАРУЖЕНЫ АНОМАЛИИ</b>")
        lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
        lines.append(
            f"⚠️ {total_pokatushki} отправлений с обратной логистикой без продаж.\n"
            f"   Проверьте регионы — возможны атаки конкурентов или фейковые заказы."
        )

    return _render_sections(grouped, total_income, lines)


def _render_sections(grouped, total_income, lines) -> str:
    """Рендерит TOP-15, TOP-3 убыточных и футер."""
    lines.append("")
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
    lines.append("📦 <b>ПРИБЫЛЬ ПО АРТИКУЛАМ (TOP-15)</b>")
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")

    top = grouped.sort_values("чистая_прибыль", ascending=False).head(15)
    for sku, row in top.iterrows():
        shown = f"{row['артикул']} (SKU {str(sku).split(':', 1)[-1]})" if row.get("артикул") else sku
        label = f"🔻 {shown}" if row["чистая_прибыль"] < 0 else f"🔹 {shown}"
        lines.append(f"\n  <b>{label}</b>")
        lines.append(f"    💰 Доход:           {fmt_signed(row['доход'])} руб.")
        for key, label in (("комиссия", "🏷 Комиссия:     "), ("логистика", "🚚 Логистика:    "),
                           ("хранение", "📦 Хранение:     "), ("штрафы", "🔴 Штрафы:       "),
                           ("эквайринг", "💳 Эквайринг:    "), ("партнёры", "📣 Партнёры:     "),
                           ("прочее", "🧮 Прочее:       "), ("компенсации", "💚 Компенсации:  ")):
            if abs(row[key]) > 0.005 or key in ("логистика", "штрафы"):
                lines.append(f"    {label}   {fmt_signed(row[key])} руб.")
        ит = "✅" if row["чистая_прибыль"] >= 0 else "🚨"
        if row["чистая_прибыль"] < 0:
            lines.append(
                f"    {ит} <b>Чистый убыток: {fmt_profit(row['чистая_прибыль'])} руб.</b>"
            )
        else:
            lines.append(
                f"    {ит} Итого: {fmt_profit(row['чистая_прибыль'])} руб."
            )

    артикулы_only = grouped[grouped.index != ""]
    if not артикулы_only.empty:
        убыточные = артикулы_only.nsmallest(3, "чистая_прибыль")
        min_profit = убыточные.iloc[0]["чистая_прибыль"]
        header = (
            "ТОП-3 УБЫТОЧНЫХ ТОВАРА"
            if min_profit < 0
            else "ТОП-3 НАИМЕНЕЕ ПРИБЫЛЬНЫХ ТОВАРА"
        )
        lines.append("")
        lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
        lines.append(f"⚠️ <b>{header}</b>")
        lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")

        for i, (sku, row) in enumerate(убыточные.iterrows(), 1):
            причины = {
                "Комиссия":    -min(row["комиссия"], 0),
                "Логистика":   -min(row["логистика"], 0),
                "Хранение":    -min(row["хранение"], 0),
                "Штрафы":      -min(row["штрафы"], 0),
                "Эквайринг":   -min(row["эквайринг"], 0),
                "Партнёры":    -min(row["партнёры"], 0),
                "Прочее":      -min(row["прочее"], 0),
            }
            главная_причина = max(причины, key=причины.get)
            главная_сумма = причины[главная_причина]
            status_icon = "🔻" if row["чистая_прибыль"] < 0 else "⚠️"
            status = "УБЫТОК" if row["чистая_прибыль"] < 0 else "мин. прибыль"
            lines.append(f"\n  {i}. {status_icon} [{status}] <b>{sku}</b>")
            lines.append(f"     Чистая прибыль: {fmt_profit(row['чистая_прибыль'])} руб.")
            if главная_сумма > 0:
                lines.append(
                    f"     Основной расход: {главная_причина} ({fmt(главная_сумма)} руб.)"
                )
            insight = generate_insight_ozon(sku, row, total_income)
            if insight:
                lines.append(f"     {insight}")

    lines.extend(render_abc(grouped["чистая_прибыль"]))

    lines.append("")
    lines.append("📊 Присылай отчёт каждую неделю и сравнивай динамику: /compare")
    # Рекламный футер с тарифами добавляет bot.py — и только тем, кто ещё не оплатил

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python ozon_parser.py <путь_к_файлу.csv|xlsx>")
        sys.exit(1)
    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"Файл не найден: {filepath}")
        sys.exit(1)
    print(analyze(filepath))

