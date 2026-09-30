"""
report_compare.py — Сравнение двух отчётов одного маркетплейса («было → стало»).

Цифры считаются детерминированно (pandas), без LLM: так сравнение работает
всегда и бесплатно. LLM (если подключён) получает готовую сводку и лишь
добавляет выводы — поэтому не может «придумать» цифры.
"""

from __future__ import annotations

import html

import pandas as pd

TOP_N = 5

# Порядок и подписи итоговых метрик. Расходы показываем по модулю
# (в WB они положительные, в Ozon — отрицательные).
_TOTALS = [
    ("доход", "💰 Выручка", False),
    ("логистика", "🚚 Логистика", True),
    ("штрафы", "🔴 Штрафы", True),
    ("хранение", "📦 Хранение", True),
    ("приёмка", "🏭 Приёмка", True),
    ("удержания", "📋 Удержания", True),
    ("эквайринг", "💳 Эквайринг", True),
    ("партнёры", "📣 Партнёры", True),
    ("прочее", "🧮 Прочее", True),
    ("чистая_прибыль", "ЧИСТАЯ ПРИБЫЛЬ", False),
]


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def _signed(v: float) -> str:
    return ("+" if v > 0 else "−" if v < 0 else "±") + _fmt(abs(v))


def _pct(old: float, new: float) -> str:
    if abs(old) < 1e-9 or abs(new - old) < 0.5:
        return ""
    return f", {(new - old) / abs(old) * 100:+.0f}%"


def _label(sku) -> str:
    s = str(sku)
    for prefix in ("SKU:", "ART:", "ID:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def _trend_icon(delta: float, is_expense: bool) -> str:
    if abs(delta) < 0.5:
        return "➖"
    good = delta < 0 if is_expense else delta > 0
    return "🟢" if good else "🔴"


def sku_deltas(old: dict, new: dict) -> pd.DataFrame:
    """DataFrame по SKU: было, стало, дельта (по чистой прибыли)."""
    a = old["grouped"]["чистая_прибыль"].rename("было")
    b = new["grouped"]["чистая_прибыль"].rename("стало")
    df = pd.concat([a, b], axis=1)
    df = df[df.index.astype(str).str.strip() != ""]
    df["новый"] = df["было"].isna()
    df["пропал"] = df["стало"].isna()
    df = df.fillna(0.0)
    df["дельта"] = df["стало"] - df["было"]
    return df


def compare_metrics(old: dict, new: dict) -> str:
    """HTML-отчёт «было → стало» для Telegram."""
    if old.get("marketplace") != new.get("marketplace"):
        raise ValueError(
            f"Нельзя сравнить отчёты разных маркетплейсов: "
            f"{old.get('marketplace')} и {new.get('marketplace')}."
        )

    lines = [
        f"📊 <b>СРАВНЕНИЕ ОТЧЁТОВ {html.escape(str(new.get('marketplace', '')))}</b>",
        "<i>первый файл → второй файл</i>",
        "➖➖➖➖➖➖➖➖➖➖➖➖",
    ]
    for key, label, is_expense in _TOTALS:
        if key not in old and key not in new:
            continue
        o = abs(old.get(key, 0.0)) if is_expense else old.get(key, 0.0)
        n = abs(new.get(key, 0.0)) if is_expense else new.get(key, 0.0)
        if is_expense and o == 0 and n == 0:
            continue
        d = n - o
        line = f"{_trend_icon(d, is_expense)} {label}: {_fmt(o)} → {_fmt(n)} руб. ({_signed(d)}{_pct(o, n)})"
        if key == "чистая_прибыль":
            line = f"<b>{line}</b>"
        lines.append(line)

    # Доля расходов в выручке — главный индикатор «съеденной» маржи
    def _cost_share(m: dict) -> float | None:
        inc = m.get("доход", 0.0)
        if inc <= 0:
            return None
        return (inc - m.get("чистая_прибыль", 0.0)) / inc * 100

    s_old, s_new = _cost_share(old), _cost_share(new)
    if s_old is not None and s_new is not None:
        icon = _trend_icon(s_new - s_old, True)
        lines.append(f"{icon} Расходы маркетплейса: {s_old:.0f}% → {s_new:.0f}% выручки")

    df = sku_deltas(old, new)
    fallers = df[df["дельта"] < 0].nsmallest(TOP_N, "дельта")
    growers = df[df["дельта"] > 0].nlargest(TOP_N, "дельта")

    if not fallers.empty:
        lines += ["", "📉 <b>Просели сильнее всего:</b>"]
        for sku, r in fallers.iterrows():
            tag = " <i>(пропал из отчёта)</i>" if r["пропал"] else ""
            lines.append(
                f"  🔻 {html.escape(_label(sku))}: {_fmt(r['было'])} → {_fmt(r['стало'])} "
                f"({_signed(r['дельта'])}){tag}"
            )
    if not growers.empty:
        lines += ["", "📈 <b>Выросли сильнее всего:</b>"]
        for sku, r in growers.iterrows():
            tag = " <i>(новый)</i>" if r["новый"] else ""
            lines.append(
                f"  🔹 {html.escape(_label(sku))}: {_fmt(r['было'])} → {_fmt(r['стало'])} "
                f"({_signed(r['дельта'])}){tag}"
            )

    turned_loss = df[(df["было"] > 0) & (df["стало"] < 0)]
    if not turned_loss.empty:
        names = ", ".join(html.escape(_label(s)) for s in turned_loss.index[:TOP_N])
        lines += ["", f"🚨 <b>Ушли в минус:</b> {names}"]

    lines += ["", "➖➖➖➖➖➖➖➖➖➖➖➖"]
    return "\n".join(lines)


def compare_plain(old: dict, new: dict) -> str:
    """Компактная текстовая сводка для LLM (без HTML)."""
    parts = [f"Маркетплейс: {new.get('marketplace')}"]
    for key, label, is_expense in _TOTALS:
        if key in old or key in new:
            o = abs(old.get(key, 0.0)) if is_expense else old.get(key, 0.0)
            n = abs(new.get(key, 0.0)) if is_expense else new.get(key, 0.0)
            parts.append(f"{key}: было {o:.0f}, стало {n:.0f}")
    df = sku_deltas(old, new).sort_values("дельта")
    parts.append("Изменение чистой прибыли по SKU (было → стало, дельта):")
    for sku, r in pd.concat([df.head(10), df.tail(10)]).drop_duplicates().iterrows():
        parts.append(f"  {_label(sku)}: {r['было']:.0f} → {r['стало']:.0f} ({r['дельта']:+.0f})")
    return "\n".join(parts)
