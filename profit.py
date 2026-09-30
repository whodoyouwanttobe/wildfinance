"""
profit.py — Настоящая прибыль: сумма к выплате − себестоимость − налог.

Чистая логика без Telegram и базы данных — легко тестировать.

Снимок отчёта (snapshot) — то, что бот сохраняет после каждого отчёта:
    {
      "marketplace": "WB" | "Ozon",
      "items": [ {"article", "name", "units", "payout", "gross"} ... ],
      "general_payout": float,   # расходы без артикула (хранение, реклама…), обычно < 0
    }

Налоги (упрощённая оценка за период отчёта):
  • usn6  — УСН «доходы»: ставка × выручка (цена продажи покупателю).
  • usn15 — УСН «доходы − расходы»: ставка × (к выплате − себестоимость), не меньше 0.
  • none  — без налога.
Минимальный налог УСН (1% от доходов) считается по итогам года — здесь не учитываем.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

TAX_MODES = {
    "none": "без налога",
    "usn6": "УСН «доходы»",
    "usn15": "УСН «доходы − расходы»",
}
DEFAULT_RATES = {"none": 0.0, "usn6": 6.0, "usn15": 15.0}

MAX_ITEMS_SHOWN = 15


@dataclass
class TaxSettings:
    mode: str | None = None   # None — ещё не выбран
    rate: float = 0.0         # в процентах

    @property
    def label(self) -> str:
        if not self.mode:
            return "не указан"
        if self.mode == "none":
            return "без налога"
        return f"{TAX_MODES[self.mode]} {self.rate:g}%"


# ─── Снимок отчёта из метрик парсера ─────────────────────────────────────────

def _is_general(key: str) -> bool:
    k = str(key).strip()
    return not k or k.startswith("ID:")


def article_label(key: str, row) -> str:
    """Артикул, понятный продавцу: для Ozon — «Артикул», иначе SKU без префикса."""
    art = str(row.get("артикул", "") or "").strip() if hasattr(row, "get") else ""
    if art:
        return art
    s = str(key)
    for prefix in ("SKU:", "ART:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def snapshot_from_metrics(metrics: dict) -> dict:
    g = metrics["grouped"]
    items, general = [], 0.0
    for key, row in g.iterrows():
        payout = float(row.get("чистая_прибыль", 0) or 0)
        if _is_general(key):
            general += payout
            continue
        items.append({
            "article": article_label(key, row),
            "name": str(row.get("название", "") or "").strip(),
            "units": float(row.get("шт", 0) or 0),
            "payout": round(payout, 2),
            "gross": round(float(row.get("выручка_брутто", 0) or 0), 2),
        })
    return {"marketplace": metrics.get("marketplace", "WB"), "items": items,
            "general_payout": round(general, 2)}


def interesting_order(items: list[dict]) -> list[int]:
    """Порядок показа артикулов: сначала убыточные, затем по убыванию выплаты."""
    idx = list(range(len(items)))
    return sorted(idx, key=lambda i: (items[i]["payout"] >= 0, -abs(items[i]["payout"])))


# ─── Расчёт ─────────────────────────────────────────────────────────────────

def item_profit(item: dict, cost: float, tax: TaxSettings) -> dict:
    units = max(float(item["units"]), 0.0)
    cost_total = cost * units
    before_tax = item["payout"] - cost_total
    gross = max(float(item.get("gross", 0) or 0), 0.0)
    rate = (tax.rate or 0) / 100 if tax.mode in ("usn6", "usn15") else 0.0
    if tax.mode == "usn6":
        tax_sum = rate * gross
    elif tax.mode == "usn15":
        tax_sum = rate * max(before_tax, 0.0)
    else:
        tax_sum = 0.0
    profit = before_tax - tax_sum
    return {
        **item,
        "cost": cost,
        "cost_total": cost_total,
        "tax": tax_sum,
        "profit": profit,
        "margin": (profit / gross * 100) if gross > 0 else None,
        "roi": (profit / cost_total * 100) if cost_total > 0 else None,
        "per_unit": (profit / units) if units > 0 else None,
    }


def build_profit(snapshot: dict, costs: dict[str, float], tax: TaxSettings) -> dict:
    """Прибыль по артикулам, для которых известна себестоимость, и итог."""
    items = snapshot["items"]
    rows = [item_profit(it, costs[it["article"]], tax) for it in items if it["article"] in costs]
    covered = len(rows)
    total = {
        "payout": sum(r["payout"] for r in rows),
        "cost_total": sum(r["cost_total"] for r in rows),
        "units": sum(max(r["units"], 0) for r in rows),
        "gross": sum(max(r.get("gross", 0), 0) for r in rows),
        "tax": sum(r["tax"] for r in rows),
    }
    full = covered == len(items) and covered > 0
    general = float(snapshot.get("general_payout", 0) or 0)
    if full and general:
        # Общие расходы без артикула (хранение, реклама) уменьшают прибыль магазина,
        # а при УСН «доходы − расходы» — и налоговую базу
        total["payout"] += general
        if tax.mode == "usn15":
            base = total["payout"] - total["cost_total"]
            total["tax"] = (tax.rate / 100) * max(base, 0.0)
    total["profit"] = total["payout"] - total["cost_total"] - total["tax"]
    total["margin"] = (total["profit"] / total["gross"] * 100) if total["gross"] > 0 else None
    total["roi"] = (total["profit"] / total["cost_total"] * 100) if total["cost_total"] > 0 else None
    return {"rows": rows, "covered": covered, "total_items": len(items), "full": full,
            "general_payout": general, "total": total}


# ─── Отображение ────────────────────────────────────────────────────────────

def _rub(v: float) -> str:
    sign = "−" if v < 0 else ""
    return f"{sign}{abs(v):,.0f}".replace(",", " ") + " ₽"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.0f}%"


def render_item_card(r: dict, tax: TaxSettings) -> str:
    esc = html.escape
    title = esc(r["article"]) + (f" — {esc(r['name'])}" if r.get("name") else "")
    icon = "✅" if r["profit"] >= 0 else "🔻"
    lines = [
        f"🔹 <b>{title}</b>",
        f"   Продано: {r['units']:g} шт",
        f"   К выплате:      {_rub(r['payout'])}",
        f"   Себестоимость: {_rub(-r['cost_total'])} ({r['cost']:,.0f} × {max(r['units'], 0):g})".replace(",", " "),
    ]
    if tax.mode in ("usn6", "usn15"):
        lines.append(f"   Налог ({tax.label}): {_rub(-r['tax'])}")
    lines.append(f"   {icon} <b>Прибыль: {_rub(r['profit'])}</b> · маржа {_pct(r['margin'])}")
    if r["per_unit"] is not None:
        lines.append(f"   💵 С одной штуки: {_rub(r['per_unit'])}")
    if r["units"] > 0:
        # Безубыточность: какая выплата за штуку нужна, чтобы покрыть себестоимость и налог
        need = r["cost"] + (r["tax"] / r["units"] if r["units"] else 0)
        have = r["payout"] / r["units"]
        lines.append(f"   📍 Выплата за штуку: {_rub(have)} · нужно ≥ {_rub(need)} для нуля")
    if r["profit"] < 0 and r["units"] > 0:
        lines.append("   🚨 <b>Продаёте в минус</b> — поднимите цену или снизьте расходы на логистику.")
    elif r["margin"] is not None and 0 <= r["margin"] < 10:
        lines.append("   ⚠️ Маржа ниже 10% — любой рост тарифов уведёт товар в минус.")
    if not tax.mode:
        lines.append("   <i>Налог не учтён — выбери систему налогообложения кнопкой ниже.</i>")
    return "\n".join(lines)


def render_profit_block(p: dict, tax: TaxSettings) -> str:
    esc = html.escape
    t = p["total"]
    lines = ["➖➖➖➖➖➖➖➖➖➖➖➖", "📦 <b>ПРИБЫЛЬ С УЧЁТОМ СЕБЕСТОИМОСТИ</b>", "➖➖➖➖➖➖➖➖➖➖➖➖"]
    if not p["covered"]:
        lines.append("Себестоимость для товаров из этого отчёта пока не указана.")
        return "\n".join(lines)
    scope = "по всему магазину" if p["full"] else f"по {p['covered']} из {p['total_items']} артикулов"
    lines += [
        f"<i>{scope}</i>",
        f"💰 К выплате:        {_rub(t['payout'])}",
        f"📦 Себестоимость ({t['units']:g} шт): {_rub(-t['cost_total'])}",
    ]
    if tax.mode in ("usn6", "usn15"):
        lines.append(f"🧾 Налог ({tax.label}): {_rub(-t['tax'])}")
    icon = "✅" if t["profit"] >= 0 else "🚨"
    lines.append(f"{icon} <b>ЧИСТАЯ ПРИБЫЛЬ: {_rub(t['profit'])}</b>")
    lines.append(f"📈 Маржа: {_pct(t['margin'])} · ROI: {_pct(t['roi'])}")
    if not p["full"]:
        missing = p["total_items"] - p["covered"]
        lines.append(f"<i>Ещё {missing} арт. без себестоимости — их прибыль не посчитана.</i>")
        if p["general_payout"]:
            lines.append(f"<i>Общие расходы без артикула (хранение, реклама): {_rub(p['general_payout'])}"
                         " — учтутся, когда будет указана себестоимость всех товаров.</i>")
    if not tax.mode:
        lines.append("<i>Налог не учтён — выбери систему налогообложения кнопкой ниже.</i>")

    rows = sorted(p["rows"], key=lambda r: r["profit"])
    lines += ["", "<b>По артикулам</b> (сначала худшие):"]
    for r in rows[:MAX_ITEMS_SHOWN]:
        icon = "🔻" if r["profit"] < 0 else ("⚠️" if (r["margin"] or 0) < 10 else "🔹")
        per = f" · {_rub(r['per_unit'])}/шт" if r["per_unit"] is not None else ""
        lines.append(f"{icon} {esc(r['article'])}: {_rub(r['profit'])} ({_pct(r['margin'])}){per}")
    if len(rows) > MAX_ITEMS_SHOWN:
        lines.append(f"… и ещё {len(rows) - MAX_ITEMS_SHOWN}")
    losers = [r for r in rows if r["profit"] < 0 and r["units"] > 0]
    if losers:
        lines += ["", f"🚨 <b>{len(losers)} товар(ов) продаются в минус</b> с учётом закупки. "
                      "Это главное, что стоит исправить."]
    return "\n".join(lines)


# ─── Разбор ввода себестоимости ─────────────────────────────────────────────

_NUM_RE = re.compile(r"^-?\d[\d\s ]*(?:[.,]\d+)?$")


def parse_money(text: str) -> float | None:
    """'850' / '1 200,50' / '850 р' / '850₽' → число. '-', '0', пусто → None (пропуск)."""
    s = (text or "").strip().lower()
    s = re.sub(r"(руб(лей|\.)?|р\.?|₽)$", "", s).strip()
    if s in ("", "-", "—", "0", "нет", "пропуск"):
        return None
    if not _NUM_RE.match(s):
        raise ValueError(f"Не понимаю число: «{text}»")
    v = float(s.replace(" ", "").replace(" ", "").replace(",", "."))
    if v < 0 or v > 10_000_000:
        raise ValueError(f"Странная себестоимость: {text}")
    return v if v > 0 else None


def parse_cost_list(text: str, count: int) -> dict[int, float]:
    """
    Ответ на пронумерованный список из count артикулов.
    Понимает:
      • «850, 320, -, 540» или по одному числу в строке — по порядку;
      • «3 540» / «3: 540» / «3) 540» / «3. 540» — номер и цена.
    Возвращает {индекс (с 0): цена}. Пропущенные (-, 0) не попадают.
    """
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    strict = re.compile(r"^(\d{1,3})\s*[.):]\s*(.+)$")        # «3) 540», «3: 540», «3. 540»
    loose = re.compile(r"^(\d{1,3})\s+(\S.*)$")                # «3 540»

    def _numbered(rx) -> dict[int, float]:
        result = {}
        for ln in lines:
            n, val = rx.match(ln).groups()
            idx = int(n) - 1
            if not 0 <= idx < count:
                raise ValueError(f"Нет товара с номером {n}.")
            v = parse_money(val)
            if v is not None:
                result[idx] = v
        return result

    if lines and all(strict.match(ln) for ln in lines):
        return _numbered(strict)
    parts = [x for x in re.split(r"[,;\n]+", text or "") if x.strip()]
    if len(parts) != count:
        # «3 540» по строкам — номер и цена (когда значений не столько, сколько товаров)
        if lines and all(loose.match(ln) for ln in lines):
            return _numbered(loose)
        raise ValueError(f"Нужно {count} значений по порядку, а получил {len(parts)}.")
    result = {}
    for i, part in enumerate(parts):
        v = parse_money(part)
        if v is not None:
            result[i] = v
    return result
