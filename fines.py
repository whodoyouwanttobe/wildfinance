"""
fines.py — расшифровка штрафов и удержаний + текст обращения в поддержку.

Источники:
  • WB, детализация: строки, где «Общая сумма штрафов» ≠ 0 или обоснование
    содержит «штраф» (это ШТРАФЫ — их можно оспаривать), и строки, где
    «Удержания» ≠ 0 (реклама, подписки, платный тариф — обычно не оспариваются).
  • Ozon, детализация начислений: строки категории «штрафы».

Каждая позиция: kind («Штраф» / «Удержание»), дата, артикул, тип, сумма (>0),
идентификаторы для поддержки (Srid, сборочное задание, ШК, ID начисления).

Названия колонок вынесены в константы — если в реальных отчётах они другие,
поправить нужно только здесь.
"""

from __future__ import annotations

import html
import os
import tempfile
from collections import OrderedDict

import pandas as pd

from table_reader import to_num

MAX_STORED = 300          # сколько позиций храним в снимке отчёта
APPEAL_MAX_CHARS = 1000   # лимит текста обращения (как в форме поддержки WB)

WB_FINE_SUM = "Общая сумма штрафов"
WB_HOLD_SUM = "Удержания"
WB_REASON = "Обоснование для оплаты"
WB_TYPE_COLUMNS = ("Виды доставок, штрафов и корректировок ВВ", "Виды логистики, штрафов и доплат")
WB_DATE_COLUMNS = ("Дата продажи", "Дата операции", "Дата заказа покупателем")
WB_ID_COLUMNS = {
    "srid": ("Srid",),
    "задание": ("Номер сборочного задания",),
    "шк": ("ШК", "Шк"),
    "nm": ("Код номенклатуры",),
}

OZON_DATE_COLUMNS = ("Дата начисления", "Дата операции")


def _first(df: pd.DataFrame, names) -> str | None:
    return next((c for c in names if c in df.columns), None)


def _txt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s in ("nan", "NaT", "None", "0") else s


def _date(v) -> str:
    s = _txt(v)
    return s[:10]


def from_wb(df: pd.DataFrame) -> list[dict]:
    """Штрафы и удержания из детализации WB."""
    if df is None or df.empty:
        return []
    fine = to_num(df[WB_FINE_SUM]) if WB_FINE_SUM in df.columns else pd.Series(0.0, index=df.index)
    hold = to_num(df[WB_HOLD_SUM]) if WB_HOLD_SUM in df.columns else pd.Series(0.0, index=df.index)
    reason = df[WB_REASON].astype(str).str.lower() if WB_REASON in df.columns else pd.Series("", index=df.index)
    type_col = _first(df, WB_TYPE_COLUMNS)
    date_col = _first(df, WB_DATE_COLUMNS)
    art_col = "Артикул поставщика" if "Артикул поставщика" in df.columns else None
    ids = {k: _first(df, v) for k, v in WB_ID_COLUMNS.items()}

    is_fine = (fine != 0) | reason.str.contains("штраф", na=False)
    is_hold = (hold != 0) & ~is_fine
    out: list[dict] = []
    for idx in df.index[is_fine | is_hold]:
        kind = "Штраф" if is_fine[idx] else "Удержание"
        amount = abs(float(fine[idx] if kind == "Штраф" else hold[idx]))
        if amount == 0:
            continue
        typ = _txt(df.at[idx, type_col]) if type_col else ""
        out.append({
            "kind": kind,
            "date": _date(df.at[idx, date_col]) if date_col else "",
            "article": _txt(df.at[idx, art_col]) if art_col else "",
            "type": typ or ("Штраф" if kind == "Штраф" else "Удержание"),
            "amount": round(amount, 2),
            "srid": _txt(df.at[idx, ids["srid"]]) if ids["srid"] else "",
            "task": _txt(df.at[idx, ids["задание"]]) if ids["задание"] else "",
            "barcode": _txt(df.at[idx, ids["шк"]]) if ids["шк"] else "",
            "nm": _txt(df.at[idx, ids["nm"]]) if ids["nm"] else "",
        })
    return out


def from_ozon(df: pd.DataFrame) -> list[dict]:
    """Штрафы из детализации начислений Ozon (категория «штрафы»)."""
    if df is None or df.empty or "_категория" not in df.columns:
        return []
    date_col = _first(df, OZON_DATE_COLUMNS)
    out = []
    for idx in df.index[df["_категория"] == "штрафы"]:
        amount = abs(float(df.at[idx, "_сумма"]))
        if amount == 0:
            continue
        out.append({
            "kind": "Штраф",
            "date": _date(df.at[idx, date_col]) if date_col else "",
            "article": _txt(df.at[idx, "Артикул"]) if "Артикул" in df.columns else "",
            "type": _txt(df.at[idx, "_тип"]) or "Штраф",
            "amount": round(amount, 2),
            "srid": _txt(df.at[idx, "_id"]),
            "task": "",
            "barcode": "",
            "nm": _txt(df.at[idx, "SKU"]) if "SKU" in df.columns else "",
        })
    return out


# ─── Сводка ─────────────────────────────────────────────────────────────────

def totals(items: list[dict]) -> dict:
    fines = [i for i in items if i["kind"] == "Штраф"]
    holds = [i for i in items if i["kind"] == "Удержание"]
    return {
        "fines_count": len(fines), "fines_sum": round(sum(i["amount"] for i in fines), 2),
        "holds_count": len(holds), "holds_sum": round(sum(i["amount"] for i in holds), 2),
    }


def _fmt(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


def _by(items: list[dict], key: str) -> list[tuple[str, int, float]]:
    agg: dict[str, list] = OrderedDict()
    for i in items:
        k = i[key] or "—"
        a = agg.setdefault(k, [0, 0.0])
        a[0] += 1
        a[1] += i["amount"]
    return sorted(((k, c, s) for k, (c, s) in agg.items()), key=lambda x: -x[2])


def summary_line(items: list[dict]) -> str:
    """Одна строка под отчётом."""
    t = totals(items)
    parts = []
    if t["fines_count"]:
        parts.append(f"штрафов {t['fines_count']} на {_fmt(t['fines_sum'])} ₽")
    if t["holds_count"]:
        parts.append(f"удержаний {t['holds_count']} на {_fmt(t['holds_sum'])} ₽")
    if not parts:
        return ""
    return "\n\n🔍 В отчёте " + ", ".join(parts) + " — расшифровка кнопкой «Штрафы и удержания» ниже."


def render(items: list[dict], marketplace: str = "WB") -> str:
    """HTML-расшифровка для Telegram."""
    if not items:
        return "✅ В последнем отчёте нет штрафов и удержаний."
    esc = html.escape
    t = totals(items)
    lines = [f"🔍 <b>Штрафы и удержания — {esc(marketplace)}</b>"]
    fines = [i for i in items if i["kind"] == "Штраф"]
    holds = [i for i in items if i["kind"] == "Удержание"]

    if fines:
        lines += ["", f"🔴 <b>Штрафы: {t['fines_count']} шт. на {_fmt(t['fines_sum'])} ₽</b>"]
        for typ, c, s in _by(fines, "type")[:10]:
            lines.append(f"• {esc(typ)} — {c} шт., {_fmt(s)} ₽")
        arts = [a for a in _by(fines, "article") if a[0] != "—"]
        if arts:
            lines.append("<i>По артикулам:</i> " + ", ".join(
                f"{esc(a)} — {_fmt(s)} ₽" for a, c, s in arts[:5]))
        lines += ["", "<b>Последние позиции:</b>"]
        for i in sorted(fines, key=lambda x: x["date"], reverse=True)[:10]:
            ref = i["srid"] or i["task"]
            lines.append(
                f"• {esc(i['date'] or '—')} · {esc(i['article'] or 'без артикула')} · "
                f"{esc(i['type'])} · <b>{_fmt(i['amount'])} ₽</b>"
                + (f"\n   <code>{esc(ref)}</code>" if ref else "")
            )
        if len(fines) > 10:
            lines.append(f"…и ещё {len(fines) - 10} — полный список в Excel.")

    if holds:
        lines += ["", f"🟠 <b>Удержания: {t['holds_count']} шт. на {_fmt(t['holds_sum'])} ₽</b>"]
        for typ, c, s in _by(holds, "type")[:10]:
            lines.append(f"• {esc(typ)} — {c} шт., {_fmt(s)} ₽")
        lines.append("<i>Обычно это реклама, подписки и платные тарифы — проверьте, что вы их подключали.</i>")

    if fines:
        lines += ["", "📝 Если штраф выглядит необоснованным — нажмите «Текст обращения»: "
                      "подготовлю обращение в поддержку с номерами позиций."]
    return "\n".join(lines)


# ─── Обращение в поддержку ──────────────────────────────────────────────────

def _positions(fines: list[dict], limit: int = 8) -> list[str]:
    rows = []
    for i in sorted(fines, key=lambda x: -x["amount"])[:limit]:
        ref = i["srid"] or i["task"] or i["barcode"]
        rows.append(
            f"- {i['date'] or 'дата не указана'}, {i['type']}, {i['amount']:.2f} ₽"
            + (f", артикул {i['article']}" if i["article"] else "")
            + (f", ID {ref}" if ref else "")
        )
    return rows


def appeal_template(fines: list[dict], marketplace: str = "WB") -> str:
    """Шаблон обращения без AI. Всегда укладывается в лимит: лишние позиции — «и ещё N»."""
    total = sum(i["amount"] for i in fines)
    head = "Здравствуйте! В еженедельном отчёте мне начислены штрафы, с которыми я не согласен(на):\n"
    foot = (
        f"\nИтого: {total:.2f} ₽ ({len(fines)} поз.).\n"
        "Прошу предоставить основания начисления (акты, фото, данные обмера) по каждой позиции "
        "и отменить штрафы, если нарушение не подтверждено. Спасибо!"
    )
    rows = _positions(fines, limit=len(fines))
    for n in range(len(rows), -1, -1):
        rest = len(fines) - n
        body = "\n".join(rows[:n]) + (f"\n…и ещё {rest} поз. (список приложу)" if rest else "")
        text = head + body + foot
        if len(text) <= APPEAL_MAX_CHARS:
            return text
    return trim(head + foot)


def trim(text: str, limit: int = APPEAL_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > limit // 2 else cut).rstrip() + "…"


def appeal_prompt(fines: list[dict], marketplace: str) -> tuple[str, str]:
    system = (
        "Ты помогаешь продавцу маркетплейса составить вежливое и конкретное обращение в поддержку "
        "с просьбой проверить и отменить штрафы. Пиши от первого лица продавца, деловым языком, "
        f"без эмоций и угроз. Не выдумывай фактов, которых нет в данных. Не более {APPEAL_MAX_CHARS - 100} символов. "
        "Структура: приветствие; перечень позиций (дата, тип, сумма, ID); просьба предоставить основания "
        "(акты, фото, данные обмера) и отменить штраф, если нарушение не подтверждено; благодарность. "
        "Верни только текст обращения."
    )
    total = sum(i["amount"] for i in fines)
    prompt = (
        f"Маркетплейс: {marketplace}\nШтрафы ({len(fines)} шт., всего {total:.2f} ₽):\n"
        + "\n".join(_positions(fines, limit=10))
    )
    return system, prompt


def to_excel(items: list[dict], marketplace: str = "WB") -> str:
    """Excel со всеми позициями. Возвращает путь к временному файлу (удалить после отправки)."""
    cols = OrderedDict([
        ("kind", "Вид"), ("date", "Дата"), ("article", "Артикул"), ("nm", "Код номенклатуры / SKU"),
        ("type", "Тип"), ("amount", "Сумма, ₽"), ("srid", "Srid / ID начисления"),
        ("task", "Сборочное задание"), ("barcode", "ШК"),
    ])
    df = pd.DataFrame([{v: i.get(k, "") for k, v in cols.items()} for i in items], columns=list(cols.values()))
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix=f"fines_{marketplace}_")
    os.close(fd)
    df.to_excel(path, index=False, sheet_name="Штрафы и удержания")
    return path
