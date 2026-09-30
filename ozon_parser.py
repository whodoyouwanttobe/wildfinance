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
STORAGE_TYPES = ("хранение", "утилизация")
PENALTY_TYPES = (
    "штраф", "неустойка", "удержание", "потеря", "порча",
    "подмена", "пересорт", "компенсация",
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
        return df[col_name].astype(str).fillna("")
    return pd.Series("", index=df.index)


def safe_num_col(df: pd.DataFrame, col_name: str) -> pd.Series:
    if col_name and col_name in df.columns:
        return pd.to_numeric(df[col_name], errors="coerce").fillna(0)
    return pd.Series(0, index=df.index)


def load_report(filepath: str) -> pd.DataFrame:
    """Читает CSV или Excel. Ozon выгружается в CSV/XLSX."""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp1251", "utf-8"):
            try:
                return pd.read_csv(filepath, encoding=enc)
            except UnicodeDecodeError:
                continue
        raise ValueError("Не удалось прочитать CSV: неизвестная кодировка.")
    elif ext in (".xlsx", ".xls"):
        return pd.read_excel(filepath)
    else:
        raise ValueError(f"Неподдерживаемый формат файла: {ext}. Нужен .csv или .xlsx")


def fmt(value: float) -> str:
    return f"{value:,.2f}"


def bold(text: str) -> str:
    return f"<b>{text}</b>"


def fmt_expense(value: float) -> str:
    if value > 0:
        return bold(f"-{fmt(value)}")
    return fmt(value)


def fmt_profit(value: float) -> str:
    if value < 0:
        return bold(fmt(value))
    return fmt(value)


def categorize(group: str, type_: str) -> str:
    """Возвращает: доход / логистика / хранение / штрафы / эквайринг / партнёры / прочее."""
    g = (group or "").lower().strip()
    t = (type_ or "").lower().strip()
    if "продажа" in t or "возмещение" in t or g == "продажи":
        return "доход"
    if any(s in t for s in PENALTY_TYPES):
        return "штрафы"
    if any(s in t for s in ACQUIRING_TYPES):
        return "эквайринг"
    if any(s in t for s in PARTNER_TYPES):
        return "партнёры"
    if any(s in t for s in STORAGE_TYPES):
        return "хранение"
    if any(s in t for s in LOGISTICS_TYPES):
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
    acquiring = float(row.get("эквайринг", 0))
    partner = float(row.get("партнёры", 0))
    drop_off = float(row.get("дроп_офф", 0))
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
    for cat in ("доход", "логистика", "хранение", "штрафы", "эквайринг", "партнёры", "прочее"):
        if cat not in pivot.columns:
            pivot[cat] = 0.0

    # Дополнительные агрегаты
    extra = df.groupby("_sku").agg(
        покатушки=("_покатушка", "sum"),
        дроп_офф=("_дроп_офф", "sum"),
        строк=("_сумма", "count"),
    )

    grouped = pivot.join(extra, how="outer").fillna(0)
    # Расходы в Ozon-отчёте уже отрицательные.
    # Чистая прибыль = доход − сумма |расходов|.
    expense_cols = ["логистика", "хранение", "штрафы", "эквайринг", "партнёры", "прочее"]
    grouped["чистая_прибыль"] = grouped["доход"] - grouped[expense_cols].abs().sum(axis=1)
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
    return {
        "marketplace": "Ozon",
        "доход": float(grouped["доход"].sum()),
        "логистика": float(grouped["логистика"].sum()),
        "хранение": float(grouped["хранение"].sum()),
        "штрафы": float(grouped["штрафы"].sum()),
        "эквайринг": float(grouped["эквайринг"].sum()),
        "партнёры": float(grouped["партнёры"].sum()),
        "прочее": float(grouped["прочее"].sum()),
        "чистая_прибыль": float(grouped["чистая_прибыль"].sum()),
        "grouped": grouped,
    }


def analyze(filepath: str) -> str:
    """HTML-отчёт по Ozon-отчёту (метрики считает compute())."""
    grouped = compute(filepath)["grouped"]

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
    lines.append(f"💰 Выручка (продажи):      +{fmt(total_income)} руб.")
    lines.append(f"🚚 Логистика (вся):         {fmt_expense(total_logistics)} руб.")
    if total_drop_off > 0:
        lines.append(f"    └ 📦 в т.ч. Drop-off:  {fmt_expense(total_drop_off)} руб.")
    lines.append(f"📦 Хранение + утилизация:   {fmt_expense(total_storage)} руб.")
    lines.append(f"🔴 Штрафы и удержания:      {fmt_expense(total_penalties)} руб.")
    if total_acquiring > 0:
        lines.append(f"💳 Эквайринг:                {fmt_expense(total_acquiring)} руб.")
    if total_partner > 0:
        lines.append(f"📣 Услуги партнёров:         {fmt_expense(total_partner)} руб.")
    if total_other > 0:
        lines.append(f"🧮 Прочие удержания:         {fmt_expense(total_other)} руб.")
    lines.append("")
    profit_icon = "✅" if total_profit >= 0 else "🚨"
    lines.append(
        f"{profit_icon} <b>ЧИСТАЯ ПРИБЫЛЬ: {fmt_profit(total_profit)} руб.</b>"
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
        label = f"🔻 {sku}" if row["чистая_прибыль"] < 0 else f"🔹 {sku}"
        lines.append(f"\n  <b>{label}</b>")
        lines.append(f"    💰 Доход:           +{fmt(row['доход'])} руб.")
        lines.append(f"    🚚 Логистика:       {fmt_expense(row['логистика'])} руб.")
        lines.append(f"    📦 Хранение:        {fmt_expense(row['хранение'])} руб.")
        lines.append(f"    🔴 Штрафы:          {fmt_expense(row['штрафы'])} руб.")
        if abs(row["эквайринг"]) > 0:
            lines.append(f"    💳 Эквайринг:       {fmt_expense(row['эквайринг'])} руб.")
        if abs(row["партнёры"]) > 0:
            lines.append(f"    📣 Партнёры:        {fmt_expense(row['партнёры'])} руб.")
        if abs(row["прочее"]) > 0:
            lines.append(f"    🧮 Прочее:          {fmt_expense(row['прочее'])} руб.")
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
                "Логистика":   abs(row["логистика"]),
                "Хранение":    abs(row["хранение"]),
                "Штрафы":      abs(row["штрафы"]),
                "Эквайринг":   abs(row["эквайринг"]),
                "Партнёры":    abs(row["партнёры"]),
                "Прочее":      abs(row["прочее"]),
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
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
    lines.append(
        "📊 <b>Это Ozon-аналитика.</b> Присылай еженедельно — увидишь динамику.\n"
        "   💎 Подписка — <b>1490 руб/мес</b> → /buy"
    )
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")

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

