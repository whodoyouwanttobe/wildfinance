"""Тесты кабинета по API (WB/Ozon), тарифов «1 разбор бесплатно» и уведомлений админу."""

import os
import shutil
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest
from cryptography.fernet import Fernet

import bot as B
import database
import mp_api as MA
import parser_dispatcher as PD
from test_bot_features import db, _msg, _cb  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def enc_key(monkeypatch):
    monkeypatch.setenv("API_ENC_KEY", Fernet.generate_key().decode())


@pytest.fixture
def api_db(db, monkeypatch):
    for name in ("save_api_token", "get_api_token", "list_api_tokens", "delete_api_token", "update_api_token",
                 "has_event", "free_reports_left", "use_free_report"):
        fn = getattr(database, name)
        monkeypatch.setattr(B, name, lambda *a, _fn=fn, **k: _fn(*a, db_path=db, **k))
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    return db


def _mock_client(monkeypatch, handler):
    monkeypatch.setattr(MA, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ─── WB: строки API → тот же разбор, что из Excel ───────────────────────────

def _wb_excel_as_api_rows() -> list[dict]:
    """Берём настоящий Excel-пример и превращаем его строки в ответ API (деньги — строками)."""
    import wb_parser as W
    df = W.load_report(os.path.join(HERE, "wb_real_report.xlsx"))
    rows = []
    for i, rec in enumerate(df.to_dict("records")):
        x = {"rrdId": i + 1}
        for field, col in MA.WB_FIELDS.items():
            if col not in df.columns:
                continue
            v = rec[col]
            if pd.isna(v):
                v = None
            if field in MA.WB_MONEY and v is not None:
                v = f"{float(v):.2f}"
            x[field] = v
        rows.append(x)
    return rows


def test_wb_api_rows_give_same_totals_as_excel():
    excel = PD.compute(os.path.join(HERE, "wb_real_report.xlsx"))
    path = MA.to_xlsx(MA.wb_rows_to_df(_wb_excel_as_api_rows()), "t_")
    try:
        api = PD.compute(path)
    finally:
        os.remove(path)
    assert api["marketplace"] == "WB"
    for key in ("доход", "логистика", "штрафы", "хранение", "чистая_прибыль"):
        assert api[key] == pytest.approx(excel[key], abs=0.01), key
    assert len(api["fines"]) == len(excel["fines"])


@pytest.mark.asyncio
async def test_wb_fetch_pages_until_204(monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        import json
        body = json.loads(req.content)
        calls.append(body)
        assert req.headers["Authorization"] == "tok"
        assert req.url.path == "/api/finance/v1/sales-reports/detailed"
        if body["rrdId"] == 0:
            return httpx.Response(200, json=[{"rrdId": 5, "forPay": "10.5"}, {"rrdId": 7, "forPay": "1"}])
        return httpx.Response(204)

    _mock_client(monkeypatch, handler)
    monkeypatch.setattr(MA, "WB_PAGE", 2)
    monkeypatch.setattr(MA.asyncio, "sleep", AsyncMock())
    rows = await MA.wb_fetch("tok", date(2026, 10, 5), date(2026, 10, 11))
    assert len(rows) == 2 and [c["rrdId"] for c in calls] == [0, 7]
    assert calls[0]["dateFrom"] == "2026-10-05" and calls[0]["period"] == "weekly"


@pytest.mark.asyncio
async def test_wb_errors(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(204))
    with pytest.raises(MA.NoData):
        await MA.wb_fetch("tok", date(2026, 10, 5), date(2026, 10, 11))
    _mock_client(monkeypatch, lambda req: httpx.Response(401))
    with pytest.raises(MA.ApiError) as e:
        await MA.wb_fetch("tok", date(2026, 10, 5), date(2026, 10, 11))
    assert e.value.auth
    _mock_client(monkeypatch, lambda req: httpx.Response(429, headers={"X-Ratelimit-Retry": "43200"}))
    with pytest.raises(MA.ApiError) as e:
        await MA.wb_fetch("tok", date(2026, 10, 5), date(2026, 10, 11))
    assert e.value.retry_after == 43200 and "12 ч" in str(e.value)


def test_wb_token_info():
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000000, "s": 1 << 30}).encode()).decode().rstrip("=")
    token = "eyJhbGciOiJFUzI1NiJ9." + payload + "." + "x" * 80
    info = MA.wb_token_info(token)
    assert info["read_only"] is True and info["exp"].year == 2030
    assert MA.looks_like_wb_token(token) and not MA.looks_like_wb_token("abc")


# ─── Ozon: начисления by-day → «Детализация начислений» ─────────────────────

TYPES = {"1": ("Acquiring", "Эквайринг"), "29": ("LastMileCourier", "Доставка до места выдачи"),
         "32": ("Logistic", "Логистика"), "91": ("DefectFineShipmentDelay", "Просрочка отгрузки"),
         "46": ("Placements", "Размещение")}

ACCRUALS = [
    {"accrual_id": 58880166926, "date": "2026-10-06", "unit_number": "0193082169-0206-1",
     "accrued_category": "POSTING", "total_amount": {"amount": "56.07", "currency": "RUB"},
     "posting": {"delivery_schema": "Fbo", "products": [{
         "sku": 3921245627,
         "delivery": {"total_accrued": {"amount": "-21.33"}, "services": [
             {"type_id": 32, "accrued": {"amount": "-19.32"}}, {"type_id": 29, "accrued": {"amount": "-2.01"}}]},
         "commission": {"seller_price": {"amount": "90"}, "sale_amount": {"amount": "90"},
                        "sale_commission": {"amount": "-12.6"}, "bonus": {"amount": "22.38"}}}]}},
    {"accrual_id": 58878920645, "date": "2026-10-06", "unit_number": "80454627-0141",
     "accrued_category": "ITEM", "total_amount": {"amount": "-1.09"},
     "item_fees": {"fees": [{"sku": 3921245627, "fees": [{"type_id": 1, "accrued": {"amount": "-1.09"}}]}]}},
    {"accrual_id": 3, "date": "2026-10-07", "accrued_category": "NON_ITEM", "total_amount": {"amount": "-450"},
     "non_item_fee": {"type_id": 91, "accrued": {"amount": "-450"}}},
    {"accrual_id": 4, "date": "2026-10-07", "accrued_category": "NON_ITEM", "total_amount": {"amount": "-30"},
     "non_item_fee": {"type_id": 46, "accrued": {"amount": "-25"}}},
]


def test_ozon_rows_reconcile_and_categorize():
    rows = MA.ozon_accruals_to_rows(ACCRUALS, TYPES)
    total = sum(float(a["total_amount"]["amount"]) for a in ACCRUALS)
    assert sum(r["Сумма итого, руб"] for r in rows) == pytest.approx(total)
    types = [r["Тип начисления"] for r in rows]
    assert "Выручка" in types and "Вознаграждение за продажу" in types and "Прочие начисления" in types
    assert any(t.startswith("Штраф") for t in types)
    for r in rows:
        r["Артикул"], r["Название товара"] = ("JACKET-1", "Куртка") if r["SKU"] else ("", "")
    path = MA.to_xlsx(MA.ozon_rows_to_df(rows), "t_")
    try:
        m = PD.compute(path)
    finally:
        os.remove(path)
    assert m["marketplace"] == "Ozon"
    assert m["доход"] == pytest.approx(90) and m["комиссия"] == pytest.approx(-12.6)
    assert m["логистика"] == pytest.approx(-21.33) and m["эквайринг"] == pytest.approx(-1.09)
    assert m["штрафы"] == pytest.approx(-450) and m["чистая_прибыль"] == pytest.approx(total)
    assert [f["amount"] for f in m["fines"]] == [450.0]


@pytest.mark.asyncio
async def test_ozon_fetch_paginates_days_and_adds_articles(monkeypatch):
    import json
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content or b"{}")
        assert req.headers["Client-Id"] == "123" and req.headers["Api-Key"] == "k" * 36
        if req.url.path == "/v1/finance/accrual/types":
            return httpx.Response(200, json={"accrual_types": [
                {"id": int(k), "name": v[0], "description": v[1]} for k, v in TYPES.items()]})
        if req.url.path == "/v1/finance/accrual/by-day":
            seen.append((body["date"], body["last_id"]))
            if body["date"] == "2026-10-06" and body["last_id"] == "":
                return httpx.Response(200, json={"accruals": ACCRUALS[:1], "last_id": "p2"})
            if body["date"] == "2026-10-06" and body["last_id"] == "p2":
                return httpx.Response(200, json={"accruals": ACCRUALS[1:2], "last_id": ""})
            return httpx.Response(200, json={"accruals": [], "last_id": ""})
        if req.url.path == "/v3/product/info/list":
            return httpx.Response(200, json={"items": [{"sku": 3921245627, "offer_id": "JACKET-1", "name": "Куртка"}]})
        return httpx.Response(404)

    _mock_client(monkeypatch, handler)
    monkeypatch.setattr(MA.asyncio, "sleep", AsyncMock())
    rows = await MA.ozon_fetch("123", "k" * 36, date(2026, 10, 6), date(2026, 10, 7))
    assert seen == [("2026-10-06", ""), ("2026-10-06", "p2"), ("2026-10-07", "")]
    assert {r["Артикул"] for r in rows if r["SKU"]} == {"JACKET-1"}


@pytest.mark.asyncio
async def test_ozon_check_requires_finance_role(monkeypatch):
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={
        "expires_at": "2027-04-01T00:00:00Z", "roles": [{"name": "Product", "methods": ["/v3/product/list"]}]}))
    with pytest.raises(MA.ApiError) as e:
        await MA.ozon_check("123", "k" * 36)
    assert "Admin read only" in str(e.value)
    _mock_client(monkeypatch, lambda req: httpx.Response(200, json={
        "expires_at": "2027-04-01T00:00:00Z",
        "roles": [{"name": "Admin read only", "methods": ["/v1/finance/accrual/by-day"]}]}))
    assert (await MA.ozon_check("123", "k" * 36))["expires_at"].startswith("2027")


def test_parse_ozon_credentials():
    key = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert MA.parse_ozon_credentials(f"Client ID: 123456\nAPI: {key}") == ("123456", key)
    assert MA.parse_ozon_credentials(f"{key} 123456") == ("123456", key)
    assert MA.parse_ozon_credentials("привет") is None


def test_encryption_roundtrip(enc_key):
    s = MA.encrypt({"token": "secret"})
    assert "secret" not in s and MA.decrypt(s) == {"token": "secret"}


def test_last_full_week():
    assert MA.last_full_week(date(2026, 10, 15)) == (date(2026, 10, 5), date(2026, 10, 11))
    assert MA.week_key(date(2026, 10, 5)) == "2026-W41"


# ─── Бот: подключение, отчёт по API, автоотчёт ──────────────────────────────

@pytest.mark.asyncio
async def test_connect_wb_saves_encrypted_and_deletes_message(api_db, enc_key, monkeypatch):
    monkeypatch.setattr(MA, "wb_check", AsyncMock(return_value={"exp": None, "read_only": True}))
    token = "a." + "b" * 120 + ".c"
    c = _cb(901, "api_mp_WB")
    await B.callback_api_mp(c)
    assert B._api_state[901] == "WB"
    m = _msg(901, text=token)
    await B.handle_other(m)
    m.delete.assert_awaited()
    row = database.get_api_token(901, "WB", db_path=api_db)
    assert token not in row["secret"] and MA.decrypt(row["secret"]) == {"token": token}
    assert "WB подключён" in m.answer.call_args.args[0]
    assert 901 not in B._api_state


@pytest.mark.asyncio
async def test_connect_rejects_write_token(api_db, enc_key, monkeypatch):
    monkeypatch.setattr(MA, "wb_check", AsyncMock(return_value={"exp": None, "read_only": False}))
    B._api_state[902] = "WB"
    m = _msg(902, text="a." + "b" * 120 + ".c")
    await B.handle_other(m)
    assert "Только на чтение" in m.answer.call_args.args[0]
    assert database.get_api_token(902, "WB", db_path=api_db) is None
    B._api_state.pop(902, None)


async def _fake_fetch(mp, creds, start, end):
    path = MA.to_xlsx(pd.read_excel(os.path.join(HERE, "ozon_real_report.xlsx")), "t_")
    return path


@pytest.mark.asyncio
async def test_api_report_uses_free_report_then_paywall(api_db, enc_key, monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(MA, "fetch_report_file", _fake_fetch)
    database.add_user(903, "u", db_path=api_db)
    database.save_api_token(903, "Ozon", MA.encrypt({"client_id": "1", "api_key": "k"}), db_path=api_db)
    c = _cb(903, "api_fetch_Ozon")
    await B.callback_api_fetch(c)
    out = "\n".join(x.args[0] for x in c.message.answer.call_args_list)
    assert "ОТЧЁТ OZON" in out and "бесплатный разбор" in out
    assert database.free_reports_left(903, db_path=api_db) == 0
    assert database.get_api_token(903, "Ozon", db_path=api_db)["last_sent_week"]
    c2 = _cb(903, "api_fetch_Ozon")
    await B.callback_api_fetch(c2)
    assert "Бесплатный разбор уже использован" in c2.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_auto_report_only_paid_once_per_week(api_db, enc_key, monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(MA, "fetch_report_file", _fake_fetch)
    sent = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", sent, raising=False)
    B._auto_attempts.clear()
    for uid in (904, 905):
        database.add_user(uid, "u", db_path=api_db)
        database.save_api_token(uid, "Ozon", MA.encrypt({"client_id": "1", "api_key": "k"}), db_path=api_db)
    database.extend_access(904, 30, db_path=api_db)          # 905 без подписки — автоотчёта нет
    tuesday = datetime(2026, 10, 13, 12, 0, tzinfo=B.MSK)
    assert await B.run_auto_reports(tuesday) == 1
    assert {c.args[0] for c in sent.call_args_list} == {904}
    assert await B.run_auto_reports(tuesday) == 0                 # уже отправлен за эту неделю
    monday = datetime(2026, 10, 12, 12, 0, tzinfo=B.MSK)
    assert await B.run_auto_reports(monday) == 0


@pytest.mark.asyncio
async def test_api_auth_error_turns_off_auto(api_db, enc_key, monkeypatch):
    async def bad(*a):
        raise MA.ApiError("WB не принял токен", auth=True)
    monkeypatch.setattr(MA, "fetch_report_file", bad)
    database.add_user(906, "u", db_path=api_db)
    database.extend_access(906, 30, db_path=api_db)
    database.save_api_token(906, "WB", MA.encrypt({"token": "t"}), db_path=api_db)
    c = _cb(906, "api_fetch_WB")
    await B.callback_api_fetch(c)
    row = database.get_api_token(906, "WB", db_path=api_db)
    assert row["auto_weekly"] == 0 and "не принял" in row["last_error"]


@pytest.mark.asyncio
async def test_disconnect_and_delete_me(api_db, enc_key):
    database.add_user(907, "u", db_path=api_db)
    database.save_api_token(907, "WB", MA.encrypt({"token": "t"}), db_path=api_db)
    m = _msg(907, text="/disconnect")
    await B.cmd_disconnect(m)
    assert database.list_api_tokens(907, db_path=api_db) == []
    database.save_api_token(907, "WB", MA.encrypt({"token": "t"}), db_path=api_db)
    database.delete_user_data(907, db_path=api_db)
    assert database.list_api_tokens(907, db_path=api_db) == []


@pytest.mark.asyncio
async def test_connect_disabled_without_key(api_db, monkeypatch):
    monkeypatch.delenv("API_ENC_KEY", raising=False)
    m = _msg(908, text="/connect")
    await B.cmd_connect(m)
    assert "скоро заработает" in m.answer.call_args.args[0]


# ─── Уведомления админу ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_notified_on_new_user_and_new_link(api_db, monkeypatch):
    monkeypatch.setattr(B, "ADMIN_ID", 555)
    monkeypatch.setattr(B, "set_user_source", lambda uid, src: database.set_user_source(uid, src, db_path=api_db))
    sent = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", sent, raising=False)
    await B.cmd_start(_msg(910, text="/start channel"))
    assert sent.call_args.args[0] == 555 and "Новый пользователь" in sent.call_args.args[1]
    assert "канал" in sent.call_args.args[1]
    sent.reset_mock()
    await B.cmd_start(_msg(910, text="/start channel"))          # тот же человек, та же ссылка — тишина
    sent.assert_not_awaited()
    await B.cmd_start(_msg(910, text="/start ref_910"))          # новая ссылка — сообщаем
    assert "впервые перешёл" in sent.call_args.args[1] and "реферальная" in sent.call_args.args[1]
    sent.reset_mock()
    await B.cmd_start(_msg(911, text="/start"))
    assert "без метки" in sent.call_args.args[1]


def test_describe_source():
    assert "личная ссылка" in B.describe_source("u_anna")
    assert "партнёра" in B.describe_source("p_snezhana")
    assert "метка" in B.describe_source("wbchat")


# ─── Тарифы ─────────────────────────────────────────────────────────────────

def test_year_price_is_eight_months():
    assert B.PLANS["year"]["price_rub"] == B.PLANS["month"]["price_rub"] * 8 == 7120
    assert B.year_savings() == (3560, 33)
    text = B.plan_button_text("year")
    assert "7 120" in text and "−33%" in text and "3 560" in text


# ─── Прочитал ли человек сообщение о доступе, лента активности ─────────────

@pytest.mark.asyncio
async def test_give_access_ack_and_timeline(api_db, monkeypatch):
    monkeypatch.setattr(B, "ADMIN_ID", 555)
    monkeypatch.setattr(B, "grant_access", lambda uid, d: database.grant_access(uid, d, db_path=api_db))
    monkeypatch.setattr(B, "get_crm_rows", lambda: database.get_crm_rows(api_db))
    monkeypatch.setattr(B, "get_user_events", lambda *a: database.get_user_events(*a, db_path=api_db))
    monkeypatch.setattr(B, "find_user_id", lambda q: database.find_user_id(q, db_path=api_db))
    monkeypatch.setattr(B, "log_event", lambda *a: database.log_event(*a, db_path=api_db))
    sent = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", sent, raising=False)
    database.add_user(920, "snezh", db_path=api_db)
    admin = _msg(555, text="/give_access 920 30")
    await B.cmd_give_access(admin)
    user_msg = next(c for c in sent.call_args_list if c.args[0] == 920)
    datas = [b.callback_data for r in user_msg.kwargs["reply_markup"].inline_keyboard for b in r]
    assert datas == ["access_ack", "access_howto"] and 920 in B._watch_after_grant
    sent.reset_mock()
    await B.callback_access_ack(_cb(920, "access_ack"))
    import asyncio
    await asyncio.sleep(0)
    assert any("прочитал сообщение" in c.args[1] for c in sent.call_args_list if c.args[0] == 555)
    m = _msg(555, text="/user @snezh")
    await B.cmd_user(m)
    out = m.answer.call_args.args[0]
    assert "✅ прочитал" in out and "выдан доступ" in out


def test_action_label_never_logs_free_text():
    assert B._action_label(SimpleNamespace(data="api_fetch_WB")) == "btn:api_fetch_WB"
    assert B._action_label(SimpleNamespace(data=None, document=None, text="/buy now")) == "cmd:/buy"
    assert B._action_label(SimpleNamespace(data=None, document=None, text="мой токен eyJ...")) == "text"
