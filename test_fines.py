"""Тесты расшифровки штрафов и текста обращения."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

import bot as B
import database
import fines as FN
import parser_dispatcher as PD
from test_bot_features import db, _msg, _cb  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))


def _wb():
    return PD.compute(os.path.join(HERE, "wb_real_report.xlsx"))


def test_wb_extraction():
    items = _wb()["fines"]
    t = FN.totals(items)
    assert t["fines_count"] == 8 and t["fines_sum"] == pytest.approx(10832.72, abs=0.01)
    assert t["holds_count"] > 0
    pen = [i for i in items if i["kind"] == "Штраф"]
    assert all(i["amount"] > 0 and i["type"].lower().startswith("штраф") for i in pen)
    assert all(i["srid"] for i in pen)
    assert any(i["article"] == "NW-JACKET-LITE" for i in pen)


def test_ozon_extraction():
    m = PD.compute(os.path.join(HERE, "ozon_real_report.xlsx"))
    assert [(i["kind"], i["amount"]) for i in m["fines"]] == [("Штраф", 450.0)]


def test_render_and_summary():
    items = _wb()["fines"]
    text = FN.render(items, "WB")
    assert "Штрафы: 8 шт." in text and "Удержания:" in text and "<code>" in text
    assert "штрафов 8" in FN.summary_line(items)
    assert FN.summary_line([]) == ""
    assert "нет штрафов" in FN.render([], "WB")


def test_template_fits_limit_and_keeps_request():
    pen = [i for i in _wb()["fines"] if i["kind"] == "Штраф"] * 5      # много позиций
    text = FN.appeal_template(pen, "WB")
    assert len(text) <= FN.APPEAL_MAX_CHARS
    assert "Итого" in text and "отменить штрафы" in text and "и ещё" in text


def test_trim():
    assert FN.trim("a" * 50, 100) == "a" * 50
    t = FN.trim("строка\n" * 300, 100)
    assert len(t) <= 100 and t.endswith("…")


def test_excel(tmp_path):
    items = _wb()["fines"]
    path = FN.to_excel(items, "WB")
    try:
        df = pd.read_excel(path)
        assert len(df) == len(items) and "Сумма, ₽" in df.columns
    finally:
        os.remove(path)


def test_parse_robust_to_missing_columns():
    df = pd.DataFrame({"Обоснование для оплаты": ["Штраф", "Продажа"], "Общая сумма штрафов": ["1 000,50", "0"]})
    items = FN.from_wb(df)
    assert len(items) == 1 and items[0]["amount"] == 1000.5 and items[0]["type"] == "Штраф"


# ─── Бот ────────────────────────────────────────────────────────────────────

def _save(db_path, uid, items, mp="WB"):
    database.add_user(uid, "u", db_path=db_path)
    database.save_last_report(uid, {"marketplace": mp, "items": [], "fines": items}, "r.xlsx", db_path=db_path)


@pytest.mark.asyncio
async def test_fines_without_report(db):
    m = _msg(40, "/fines")
    await B.cmd_fines(m)
    assert "Сначала пришли отчёт" in m.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_fines_callback_shows_breakdown(db):
    _save(db, 41, _wb()["fines"])
    c = _cb(41, "fines")
    await B.callback_fines(c)
    last = c.message.answer.call_args_list[-1]
    datas = [b.callback_data for row in last.kwargs["reply_markup"].inline_keyboard for b in row]
    assert datas == ["fines_appeal", "fines_xlsx"]


@pytest.mark.asyncio
async def test_appeal_uses_ai(db, monkeypatch):
    _save(db, 42, _wb()["fines"])
    ai = SimpleNamespace(available=True, ask=AsyncMock(return_value="Здравствуйте! Прошу проверить штрафы."))
    monkeypatch.setattr(B, "get_ai_client", lambda: ai)
    c = _cb(42, "fines_appeal")
    await B.callback_fines_appeal(c)
    text = c.message.answer.call_args.args[0]
    assert "<pre>Здравствуйте! Прошу проверить штрафы.</pre>" in text and "не гарантирует" in text
    system, prompt = ai.ask.call_args.args[0], ai.ask.call_args.kwargs["system"]
    assert "NW-JACKET-LITE" in system and "Не выдумывай" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("ai", [
    SimpleNamespace(available=False),
    SimpleNamespace(available=True, ask=AsyncMock(return_value="🤖 AI-анализ временно недоступен.\n...")),
    SimpleNamespace(available=True, ask=AsyncMock(side_effect=RuntimeError("down"))),
])
async def test_appeal_falls_back_to_template(db, monkeypatch, ai):
    _save(db, 43, _wb()["fines"])
    monkeypatch.setattr(B, "get_ai_client", lambda: ai)
    c = _cb(43, "fines_appeal")
    await B.callback_fines_appeal(c)
    assert "отменить штрафы" in c.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_appeal_no_penalties(db):
    _save(db, 44, [{"kind": "Удержание", "date": "", "article": "", "type": "Реклама", "amount": 10.0,
                    "srid": "", "task": "", "barcode": "", "nm": ""}])
    c = _cb(44, "fines_appeal")
    await B.callback_fines_appeal(c)
    assert "нечего" in c.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_xlsx_sent_and_removed(db, monkeypatch):
    _save(db, 45, _wb()["fines"])
    sent = {}

    async def grab(doc, caption=None):
        sent["path"] = doc.path
        assert os.path.exists(doc.path)

    c = _cb(45, "fines_xlsx")
    c.message.answer_document = grab
    await B.callback_fines_xlsx(c)
    assert not os.path.exists(sent["path"])


@pytest.mark.asyncio
async def test_report_has_fines_line_and_snapshot(db, monkeypatch):
    from test_bot_features import _send_report
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    calls = await _send_report(46, monkeypatch)          # Ozon-пример со штрафом 450 ₽
    full = "\n".join(c.args[0] for c in calls)
    assert "штрафов 1 на 450" in full and "Файл удалится" in full
    assert database.get_last_report(46, db_path=db)["fines"][0]["amount"] == 450.0


def test_prices():
    assert B.PLANS["month"]["price_rub"] == 890
    assert B.PLANS["year"]["price_rub"] < 890 * 12
