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
    ("чистая_прибыль", "К ВЫПЛАТЕ", False),
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


# ═══════════════════════════════════════════════════════════════════════════
# WB против OZON: где с рубля выручки остаётся больше
# ═══════════════════════════════════════════════════════════════════════════

def _mp_breakdown(m: dict) -> dict:
    """
    Приводит метрики WB и Ozon к одной схеме (все расходы — положительные числа):
    выручка (цена для покупателя), к выплате, комиссия, логистика, хранение,
    штрафы, реклама/прочее.
    """
    g = m["grouped"]
    payout = float(m.get("чистая_прибыль", 0))
    if m.get("marketplace") == "WB":
        gross = float(g["выручка_брутто"].sum()) if "выручка_брутто" in g else float(m.get("доход", 0))
        to_seller = float(m.get("доход", 0))  # «к перечислению» — уже за вычетом комиссии и эквайринга
        parts = {
            "комиссия": max(gross - to_seller, 0.0),
            "логистика": abs(float(m.get("логистика", 0))),
            "хранение": abs(float(m.get("хранение", 0))) + abs(float(m.get("приёмка", 0))),
            "штрафы": abs(float(m.get("штрафы", 0))),
            "реклама и прочее": float(m.get("удержания", 0)),
        }
    else:
        gross = float(m.get("доход", 0))
        neg = lambda key: -float(m.get(key, 0))  # у Ozon расходы со знаком «−»
        parts = {
            "комиссия": neg("комиссия") + neg("эквайринг"),
            "логистика": neg("логистика"),
            "хранение": neg("хранение"),
            "штрафы": neg("штрафы"),
            "реклама и прочее": neg("партнёры") + neg("прочее"),
        }
        return {"gross": gross, "payout": payout, "parts": parts,
                "comp": float(m.get("компенсации", 0))}
    return {"gross": gross, "payout": payout, "parts": parts, "comp": 0.0}


def _per_unit(m: dict) -> dict[str, dict]:
    """{артикул: {"units", "payout_per_unit", "keep_pct"}} для товаров с продажами."""
    out = {}
    g = m["grouped"]
    for key, row in g.iterrows():
        k = str(key).strip()
        if not k or k.startswith("ID:"):
            continue
        art = str(row.get("артикул", "") or "").strip() or k.split(":", 1)[-1]
        units = float(row.get("шт", 0) or 0)
        gross = float(row.get("выручка_брутто", 0) or 0)
        payout = float(row.get("чистая_прибыль", 0) or 0)
        if units <= 0 or gross <= 0:
            continue
        out[art.lower()] = {"article": art, "units": units, "ppu": payout / units,
                            "keep": payout / gross * 100}
    return out


def compare_marketplaces(a: dict, b: dict) -> str:
    """HTML-сравнение WB и Ozon (порядок файлов не важен)."""
    wb, oz = (a, b) if a.get("marketplace") == "WB" else (b, a)
    if wb.get("marketplace") != "WB" or oz.get("marketplace") != "Ozon":
        raise ValueError("Для сравнения нужны один отчёт WB и один отчёт Ozon.")
    W, O = _mp_breakdown(wb), _mp_breakdown(oz)

    def pct(v, gross):
        if gross <= 0:
            return "—"
        val = v / gross * 100
        return f"{0 if abs(val) < 0.5 else val:.0f}%"

    def row(label, w, o):
        return f"{label:<16}{w:>9}{o:>9}"

    lines = [
        "⚖️ <b>WB против OZON</b>",
        "<i>сколько съедает площадка с каждых 100 ₽ выручки</i>",
        "",
        "<pre>",
        row("", "WB", "Ozon"),
        row("Выручка, ₽", _fmt(W["gross"]), _fmt(O["gross"])),
    ]
    for key in ("комиссия", "логистика", "хранение", "штрафы", "реклама и прочее"):
        lines.append(row(key.capitalize()[:16], pct(W["parts"][key], W["gross"]), pct(O["parts"][key], O["gross"])))
    if W["comp"] > 0.5 or O["comp"] > 0.5:
        def plus(v, gross):
            return f"+{v / gross * 100:.0f}%" if gross > 0 and v > 0.5 else "0%"
        lines.append(row("Компенсации", plus(W["comp"], W["gross"]), plus(O["comp"], O["gross"])))
    keep_w = W["payout"] / W["gross"] * 100 if W["gross"] > 0 else None
    keep_o = O["payout"] / O["gross"] * 100 if O["gross"] > 0 else None
    lines += [
        row("К выплате, ₽", _fmt(W["payout"]), _fmt(O["payout"])),
        row("Остаётся с 100₽", f"{keep_w:.0f} ₽" if keep_w is not None else "—",
            f"{keep_o:.0f} ₽" if keep_o is not None else "—"),
        "</pre>",
    ]

    if keep_w is not None and keep_o is not None:
        diff = keep_o - keep_w
        if abs(diff) < 1:
            lines.append("⚖️ С рубля выручки площадки оставляют вам примерно одинаково.")
        else:
            better, worse = ("Ozon", "WB") if diff > 0 else ("WB", "Ozon")
            lines.append(f"🏆 <b>На {better} с каждых 100 ₽ выручки остаётся на {abs(diff):.0f} ₽ больше</b>, чем на {worse}.")
        # Главная статья, где разница больше всего
        diffs = {k: W["parts"][k] / W["gross"] * 100 - O["parts"][k] / O["gross"] * 100
                 for k in W["parts"]} if W["gross"] > 0 and O["gross"] > 0 else {}
        if diffs:
            k, d = max(diffs.items(), key=lambda kv: abs(kv[1]))
            if abs(d) >= 2:
                where = "WB" if d > 0 else "Ozon"
                lines.append(f"💸 Больше всего разница в статье «{k}»: на {where} она выше на {abs(d):.0f} п.п.")

    # Товары, которые продаются на обеих площадках (совпадает артикул продавца)
    pw, po = _per_unit(wb), _per_unit(oz)
    common = sorted(set(pw) & set(po), key=lambda k: -(pw[k]["units"] + po[k]["units"]))
    if common:
        lines += ["", "🔁 <b>Одинаковые товары на обеих площадках</b> (к выплате за 1 шт):"]
        for k in common[:10]:
            w, o = pw[k], po[k]
            best = "Ozon" if o["ppu"] > w["ppu"] else "WB"
            lines.append(
                f"  {html.escape(w['article'])}: WB {_fmt(w['ppu'])} ₽ · Ozon {_fmt(o['ppu'])} ₽ → "
                f"выгоднее <b>{best}</b> (+{_fmt(abs(o['ppu'] - w['ppu']))} ₽/шт)"
            )
        if len(common) > 10:
            lines.append(f"  … и ещё {len(common) - 10}")
    else:
        lines += ["", "<i>Одинаковых артикулов на обеих площадках не нашёл — сравниваю магазины целиком. "
                      "Если артикулы продавца на WB и Ozon совпадают, покажу выгоду по каждому товару.</i>"]
    lines += ["", "<i>Выплата — до себестоимости и налогов. Периоды отчётов лучше брать одинаковые.</i>"]
    return "\n".join(lines)
