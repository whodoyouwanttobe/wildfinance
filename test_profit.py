"""Себестоимость и налоги: расчёт прибыли, разбор ввода, сценарии в боте."""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import profit as P  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

SNAP = {
    "marketplace": "WB",
    "items": [
        {"article": "JEANS", "name": "Джинсы", "units": 42, "payout": 48200, "gross": 63000},
        {"article": "BELT", "name": "Ремень", "units": 10, "payout": 3000, "gross": 5000},
    ],
    "general_payout": -2000,
}


# ─── Расчёт ─────────────────────────────────────────────────────────────────

def test_item_profit_no_tax():
    r = P.item_profit(SNAP["items"][0], 850, P.TaxSettings("none", 0))
    assert r["cost_total"] == 850 * 42
    assert r["profit"] == 48200 - 35700
    assert round(r["margin"]) == round(12500 / 63000 * 100)
    assert round(r["per_unit"], 2) == round(12500 / 42, 2)


def test_usn6_from_gross_and_usn15_from_profit():
    it = SNAP["items"][0]
    r6 = P.item_profit(it, 850, P.TaxSettings("usn6", 6))
    assert r6["tax"] == pytest.approx(0.06 * 63000)
    r15 = P.item_profit(it, 850, P.TaxSettings("usn15", 15))
    assert r15["tax"] == pytest.approx(0.15 * 12500)
    loss = P.item_profit(SNAP["items"][1], 400, P.TaxSettings("usn15", 15))
    assert loss["tax"] == 0 and loss["profit"] == 3000 - 4000


def test_partial_coverage_excludes_general_expenses():
    p = P.build_profit(SNAP, {"JEANS": 850}, P.TaxSettings("none", 0))
    assert p["covered"] == 1 and not p["full"]
    assert p["total"]["profit"] == 12500
    text = P.render_profit_block(p, P.TaxSettings("none", 0))
    assert "по 1 из 2 артикулов" in text and "без себестоимости" in text


def test_full_coverage_includes_general_expenses():
    p = P.build_profit(SNAP, {"JEANS": 850, "BELT": 200}, P.TaxSettings("usn15", 15))
    assert p["full"]
    payout = 48200 + 3000 - 2000
    cost = 850 * 42 + 200 * 10
    assert p["total"]["tax"] == pytest.approx(0.15 * (payout - cost))
    assert p["total"]["profit"] == pytest.approx(payout - cost - 0.15 * (payout - cost))


def test_render_marks_losers_and_missing_tax():
    p = P.build_profit(SNAP, {"JEANS": 850, "BELT": 400}, P.TaxSettings(None, 0))
    text = P.render_profit_block(p, P.TaxSettings(None, 0))
    assert "🔻 BELT" in text and "продаются в минус" in text
    assert "Налог не учтён" in text


def test_snapshot_from_real_parser():
    from parser_dispatcher import analyze_full
    _, m = analyze_full(os.path.join(HERE, "wb_real_report.xlsx"))
    snap = P.snapshot_from_metrics(m)
    assert snap["marketplace"] == "WB" and snap["items"]
    assert all(it["article"] for it in snap["items"])
    assert snap["general_payout"] < 0          # хранение/удержания без артикула
    it = snap["items"][0]
    assert it["units"] >= 0 and it["gross"] >= it["payout"] * 0.5

    _, m2 = analyze_full(os.path.join(HERE, "ozon_real_report.xlsx"))
    snap2 = P.snapshot_from_metrics(m2)
    assert snap2["marketplace"] == "Ozon"
    assert {it["article"] for it in snap2["items"]} >= {"ART-1001"}


# ─── Разбор ввода ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("850", 850.0), ("1 200,50", 1200.5), ("850 р", 850.0), ("850₽", 850.0),
    ("-", None), ("0", None), ("", None),
])
def test_parse_money(text, expected):
    assert P.parse_money(text) == expected


def test_parse_money_rejects_garbage():
    with pytest.raises(ValueError):
        P.parse_money("восемьсот")


def test_parse_cost_list_variants():
    assert P.parse_cost_list("850, 320, -, 540", 4) == {0: 850, 1: 320, 3: 540}
    assert P.parse_cost_list("850\n1 200\n540", 3) == {0: 850, 1: 1200, 2: 540}
    assert P.parse_cost_list("3) 540\n1: 850", 4) == {2: 540, 0: 850}
    assert P.parse_cost_list("3 540", 4) == {2: 540}
    with pytest.raises(ValueError):
        P.parse_cost_list("850, 320", 3)
    with pytest.raises(ValueError):
        P.parse_cost_list("9) 100", 3)


# ─── Сценарии в боте ────────────────────────────────────────────────────────

import bot as B  # noqa: E402
import database  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    for name in ("add_user", "is_trial_active", "get_user", "extend_access", "record_payment",
                 "increment_report_count", "has_given_feedback", "save_feedback",
                 "set_costs", "get_costs", "delete_cost", "get_tax", "set_tax",
                 "save_last_report", "get_last_report", "set_user_source"):
        fn = getattr(database, name)
        monkeypatch.setattr(B, name, lambda *a, _fn=fn, **k: _fn(*a, db_path=path, **k))
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    B._cost_state.clear()
    database.save_last_report(1, SNAP, "r.xlsx", db_path=path)
    return path


def _msg(uid=1, text=None, document=None):
    m = AsyncMock()
    m.from_user = MagicMock(id=uid, username="u")
    m.text, m.caption, m.photo, m.document = text, None, None, document
    return m


def _cb(uid=1, data=""):
    c = AsyncMock()
    c.from_user = MagicMock(id=uid, username="u")
    c.data = data
    c.message = _msg(uid)
    return c


def _texts(m):
    return "\n".join(str(c.args[0]) for c in m.answer.call_args_list if c.args)


@pytest.mark.asyncio
async def test_one_article_flow(db):
    c = _cb(1, "cost_one")
    await B.callback_cost_one(c)
    kb = c.message.answer.call_args.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data.startswith("cost_pick:")
    await B.callback_cost_pick(_cb(1, "cost_pick:0"))
    assert B._cost_state[1] == {"mode": "one_value", "idx": 0}
    m = _msg(1, "850")
    await B.handle_other(m)
    out = _texts(m)
    assert "Прибыль" in out and "12 500 ₽" in out
    assert database.get_costs(1, "WB", db_path=db) == {"JEANS": 850.0}
    assert 1 not in B._cost_state


@pytest.mark.asyncio
async def test_bad_value_keeps_state(db):
    B._cost_state[1] = {"mode": "one_value", "idx": 0}
    m = _msg(1, "дорого")
    await B.handle_other(m)
    assert "Не понимаю" in _texts(m) and 1 in B._cost_state


@pytest.mark.asyncio
async def test_manual_article(db):
    B._cost_state[1] = {"mode": "manual_article"}
    m = _msg(1, "belt")
    await B.handle_other(m)
    assert B._cost_state[1] == {"mode": "one_value", "idx": 1}


@pytest.mark.asyncio
async def test_chat_list_flow_then_profit(db):
    c = _cb(1, "cost_chat")
    await B.callback_cost_chat(c)
    listing = _texts(c.message)
    assert "1. JEANS" in listing and "2. BELT" in listing
    m = _msg(1, "850, 200")
    await B.handle_other(m)
    out = _texts(m)
    assert "Сохранил себестоимость для 2" in out
    assert "ЧИСТАЯ ПРИБЫЛЬ" in out and "по всему магазину" in out


@pytest.mark.asyncio
async def test_excel_template_roundtrip(db, monkeypatch, tmp_path):
    c = _cb(1, "cost_excel")
    captured = {}

    async def grab(doc, caption=None):
        captured["df"] = pd.read_excel(doc.path)
    c.message.answer_document = grab
    await B.callback_cost_excel(c)
    df = captured["df"]
    assert list(df["Артикул"]) == ["JEANS", "BELT"] and B._cost_state[1]["mode"] == "file"
    df["Себестоимость 1 шт, ₽"] = [850, "200,5"]
    filled = tmp_path / "себестоимость_WB.xlsx"
    df.to_excel(filled, index=False)

    async def fake_download(document, destination):
        import shutil
        shutil.copy(filled, destination)
    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    m = _msg(1, document=SimpleNamespace(file_name="себестоимость_WB.xlsx", file_size=100))
    await B.handle_document(m)
    assert database.get_costs(1, "WB", db_path=db) == {"JEANS": 850.0, "BELT": 200.5}
    assert "ЧИСТАЯ ПРИБЫЛЬ" in _texts(m)


@pytest.mark.asyncio
async def test_tax_buttons_and_custom(db):
    database.set_costs(1, "WB", {"JEANS": 850}, db_path=db)
    c = _cb(1, "tax:usn6:6")
    await B.callback_tax_set(c)
    assert database.get_tax(1, db_path=db) == ("usn6", 6.0)
    assert "УСН «доходы» 6%" in _texts(c.message)
    await B.callback_tax_custom(_cb(1, "tax_custom"))
    m = _msg(1, "расходы 10")
    await B.handle_other(m)
    assert database.get_tax(1, db_path=db) == ("usn15", 10.0)


@pytest.mark.asyncio
async def test_cost_command_set_list_delete(db):
    await B.cmd_cost(_msg(1, "/cost JEANS 900"))
    assert database.get_costs(1, "WB", db_path=db) == {"JEANS": 900.0}
    m = _msg(1, "/cost")
    await B.cmd_cost(m)
    assert "JEANS — 900 ₽" in _texts(m)
    await B.cmd_cost(_msg(1, "/cost JEANS 0"))
    assert database.get_costs(1, "WB", db_path=db) == {}


@pytest.mark.asyncio
async def test_report_shows_profit_automatically_when_costs_known(db, monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    database.add_user(1, "u", db_path=db)
    database.set_costs(1, "Ozon", {"ART-1001": 500}, db_path=db)

    async def fake_download(document, destination):
        import shutil
        shutil.copy(os.path.join(HERE, "ozon_real_report.xlsx"), destination)
    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    m = _msg(1, document=SimpleNamespace(file_name="ozon.xlsx", file_size=100))
    await B.handle_document(m)
    out = _texts(m)
    assert "К ВЫПЛАТЕ ОТ OZON" in out and "ПРИБЫЛЬ С УЧЁТОМ СЕБЕСТОИМОСТИ" in out
