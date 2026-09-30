"""
abc_analysis.py — ABC-анализ ассортимента по чистой прибыли.

Классика Парето, но по ПРИБЫЛИ, а не по выручке (селлеру важны деньги,
которые остаются на руках):
  A — товары, дающие первые 80% суммарной прибыли  → «звёзды», беречь остатки
  B — следующие 15% (80–95%)                       → «середняки», растить
  C — последние 5%                                  → «хвост», кандидаты на вывод
  ❌ — убыточные (прибыль < 0)                      → в классы не входят, выводятся отдельно

Используется и WB-, и Ozon-парсером. Вход — Series «SKU → чистая прибыль».
"""

from __future__ import annotations

import html

import pandas as pd

A_THRESHOLD = 0.80
B_THRESHOLD = 0.95
MAX_LISTED = 7  # сколько артикулов показывать в каждой группе


def abc_classify(profit: pd.Series) -> pd.DataFrame:
    """
    Возвращает DataFrame (index = SKU), отсортированный по убыванию прибыли:
      прибыль, доля (доля в сумме положительной прибыли), накоп_доля, класс.
    Класс: 'A' / 'B' / 'C' для прибыльных, 'loss' для убыточных, 'zero' для нулевых.
    """
    profit = pd.to_numeric(profit, errors="coerce").fillna(0.0)
    profit = profit[profit.index.astype(str).str.strip() != ""]
    df = pd.DataFrame({"прибыль": profit}).sort_values("прибыль", ascending=False)

    positive = df["прибыль"].clip(lower=0)
    total = positive.sum()
    if total > 0:
        df["доля"] = positive / total
    else:
        df["доля"] = 0.0
    df["накоп_доля"] = df["доля"].cumsum()

    # Товар попадает в A, если ДО него накоплено < 80% (так лидер, дающий
    # 90% прибыли, остаётся в A, а не «перепрыгивает» в B).
    before = df["накоп_доля"] - df["доля"]
    cls = pd.Series("C", index=df.index)
    cls[before < B_THRESHOLD] = "B"
    cls[before < A_THRESHOLD] = "A"
    cls[df["прибыль"] == 0] = "zero"
    cls[df["прибыль"] < 0] = "loss"
    df["класс"] = cls
    return df


def _fmt(v: float) -> str:
    return f"{v:,.0f}"


def _label(sku) -> str:
    """Ozon-ключи вида 'SKU:123' / 'ART:abc' → '123' / 'abc'."""
    s = str(sku)
    for prefix in ("SKU:", "ART:", "ID:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _names(df: pd.DataFrame) -> str:
    items = [html.escape(_label(s)) for s in df.index[:MAX_LISTED]]
    rest = len(df) - len(items)
    text = ", ".join(items)
    if rest > 0:
        text += f" и ещё {rest}"
    return text


def render_abc(profit: pd.Series, basis: str = "по сумме к выплате") -> list[str]:
    """Рендерит блок ABC-анализа (список HTML-строк). Пусто, если данных мало."""
    df = abc_classify(profit)
    if len(df) < 2:
        return []

    lines = [
        "",
        "➖➖➖➖➖➖➖➖➖➖➖➖",
        f"🔤 <b>ABC-АНАЛИЗ ({basis})</b>",
        "➖➖➖➖➖➖➖➖➖➖➖➖",
    ]

    groups = [
        ("A", "🟢", "дают 80% прибыли — не допускайте out-of-stock"),
        ("B", "🟡", "стабильные середняки — тестируйте рекламу и цену"),
        ("C", "⚪️", "хвост ассортимента — кандидаты на вывод/распродажу"),
    ]
    for cls, icon, hint in groups:
        part = df[df["класс"] == cls]
        if part.empty:
            continue
        share = part["доля"].sum() * 100
        lines.append(
            f"\n{icon} <b>Группа {cls}</b> — {len(part)} шт., "
            f"{share:.0f}% прибыли ({_fmt(part['прибыль'].sum())} руб.)"
        )
        lines.append(f"   {_names(part)}")
        lines.append(f"   💡 <i>{hint}</i>")

    loss = df[df["класс"] == "loss"].sort_values("прибыль")
    if not loss.empty:
        lines.append(
            f"\n🔴 <b>Убыточные</b> — {len(loss)} шт., "
            f"минус {_fmt(-loss['прибыль'].sum())} руб."
        )
        lines.append(f"   {_names(loss)}")
        lines.append("   💡 <i>исправьте юнит-экономику или выводите — они съедают прибыль группы A</i>")

    a_count = int((df["класс"] == "A").sum())
    profitable = int(df["класс"].isin(["A", "B", "C"]).sum())
    if profitable and a_count:
        a_share = df.loc[df["класс"] == "A", "доля"].sum() * 100
        lines.append(
            f"\n📌 {a_count} из {len(df)} товаров ({a_count / len(df) * 100:.0f}% ассортимента) "
            f"приносят {a_share:.0f}% прибыли."
        )
    return lines
