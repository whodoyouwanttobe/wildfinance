"""
Тесты новых функций бота: пример отчёта, /compare, автооплата, жалобы.
Запуск: python -m pytest test_bot_features.py -v
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import bot as B  # noqa: E402
import database  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Все функции БД в bot.py работают с временной базой."""
    path = str(tmp_path / "t.db")
    for name in ("add_user", "is_trial_active", "get_user", "extend_access",
                 "record_payment", "increment_report_count", "has_given_feedback",
                 "save_feedback", "add_pending_payment", "get_pending_payments",
                 "mark_pending_paid"):
        fn = getattr(database, name)
        monkeypatch.setattr(B, name, lambda *a, _fn=fn, **k: _fn(*a, db_path=path, **k))
    return path


def _msg(user_id=1, text=None, document=None):
    m = AsyncMock()
    m.from_user = MagicMock(id=user_id, username="u")
    m.text = text
    m.caption = None
    m.photo = None
    m.document = document
    m.answer = AsyncMock()
    m.answer_document = AsyncMock()
    return m


def _cb(user_id=1, data=""):
    c = AsyncMock()
    c.from_user = MagicMock(id=user_id, username="u")
    c.data = data
    c.message = _msg(user_id)
    return c


# ─── Пример отчёта ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_demo_button_offers_choice():
    m = _msg()
    await B.cmd_demo(m)
    assert m.answer.call_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mp", ["WB", "Ozon"])
async def test_demo_sends_existing_file(mp):
    c = _cb(data=f"demo_{mp}")
    await B.callback_demo(c)
    c.message.answer_document.assert_called_once()
    sent = c.message.answer_document.call_args.args[0]
    assert os.path.exists(sent.path)
    assert B.detect_marketplace(sent.path) == mp


# ─── Жалобы: обычный текст после отчёта НЕ должен уходить админу ────────────

@pytest.mark.asyncio
async def test_text_after_report_is_not_a_complaint(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)
    B._last_file_name[7] = "r.xlsx"
    B._pending_problem.pop(7, None)
    m = _msg(7, text="спасибо")
    await B.handle_other(m)
    send.assert_not_called()


@pytest.mark.asyncio
async def test_problem_button_then_text_goes_to_admin(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)
    monkeypatch.setattr(B, "ADMIN_ID", 999)
    monkeypatch.setattr(B, "save_feedback", lambda *a, **k: None)
    B._last_file_name[8] = "report<1>.xlsx"
    await B.callback_post_problem(_cb(8, "post_problem"))
    await B.handle_other(_msg(8, text="сумма <не> сходится"))
    text = send.call_args.args[1]
    assert "report&lt;1&gt;.xlsx" in text and "&lt;не&gt;" in text
    assert 8 not in B._pending_problem


# ─── /compare ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compare_flow(db, monkeypatch):
    uid = 42
    B._compare_state.pop(uid, None)
    await B.cmd_compare(_msg(uid, text="/compare"))
    assert uid in B._compare_state

    files = iter([os.path.join(HERE, "wb_real_report.xlsx")] * 2)

    async def fake_download(document, destination):
        import shutil
        shutil.copy(next(files), destination)

    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    doc = SimpleNamespace(file_name="week.xlsx", file_size=1000)

    m1 = _msg(uid, document=doc)
    await B.handle_document(m1)
    assert "Старый отчёт принят" in m1.answer.call_args.args[0]

    m2 = _msg(uid, document=doc)
    await B.handle_document(m2)
    out = "\n".join(c.args[0] for c in m2.answer.call_args_list)
    assert "СРАВНЕНИЕ ОТЧЁТОВ WB" in out
    assert uid not in B._compare_state
    assert uid in B._last_compare


@pytest.mark.asyncio
async def test_compare_rejects_mixed_marketplaces(db, monkeypatch):
    uid = 43
    await B.cmd_compare(_msg(uid, text="/compare"))
    files = iter([os.path.join(HERE, "wb_real_report.xlsx"), os.path.join(HERE, "ozon_real_report.xlsx")])

    async def fake_download(document, destination):
        import shutil
        shutil.copy(next(files), destination)

    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    doc = SimpleNamespace(file_name="x.xlsx", file_size=1000)
    await B.handle_document(_msg(uid, document=doc))
    m2 = _msg(uid, document=doc)
    await B.handle_document(m2)
    assert "разных маркетплейсов" in m2.answer.call_args.args[0]
    assert uid in B._compare_state  # режим не сбрасывается — можно прислать правильный файл
    B._compare_state.pop(uid, None)


@pytest.mark.asyncio
async def test_cancel_exits_compare():
    B._compare_state[44] = {"old": None, "old_name": ""}
    m = _msg(44, text="/cancel")
    await B.cmd_cancel(m)
    assert 44 not in B._compare_state


# ─── Автооплата ─────────────────────────────────────────────────────────────

def test_invoice_kwargs(monkeypatch):
    monkeypatch.setattr(B, "PAYMENT_PROVIDER_TOKEN", "381764678:TEST:1")
    kw = B._build_invoice_kwargs("month", 5)
    assert kw["payload"] == "month:5"
    assert kw["currency"] == "RUB"
    assert kw["prices"][0].amount == B.PLANS["month"]["price_rub"] * 100
    assert "provider_data" not in kw


def test_invoice_with_receipt(monkeypatch):
    import json
    monkeypatch.setattr(B, "YOOKASSA_SEND_RECEIPT", True)
    kw = B._build_invoice_kwargs("forever", 5)
    receipt = json.loads(kw["provider_data"])["receipt"]
    assert receipt["items"][0]["amount"]["value"] == f"{B.PLANS['forever']['price_rub']:.2f}"
    assert kw["need_email"] is True


@pytest.mark.parametrize("payload,expected", [
    ("month:12", ("month", 12)),
    ("forever:1", ("forever", 1)),
    ("hack:1", None),
    ("month:abc", None),
    ("", None),
])
def test_parse_payload(payload, expected):
    assert B._parse_payload(payload) == expected


@pytest.mark.asyncio
async def test_pre_checkout_checks_amount():
    q = AsyncMock()
    q.invoice_payload = "month:1"
    q.currency = "RUB"
    q.total_amount = 100  # не та цена
    await B.on_pre_checkout(q)
    assert q.answer.call_args.kwargs["ok"] is False

    q.total_amount = B.PLANS["month"]["price_rub"] * 100
    await B.on_pre_checkout(q)
    assert q.answer.call_args.kwargs["ok"] is True


@pytest.mark.asyncio
async def test_successful_payment_grants_access_once(db, monkeypatch):
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    uid = 77
    # Пользователь с давно истёкшим триалом
    conn = database.get_connection(db)
    conn.execute("INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
                 (uid, "p", "2020-01-01T00:00:00"))
    conn.commit()
    conn.close()
    assert database.is_trial_active(uid, db_path=db) is False

    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)
    m = _msg(uid)
    m.successful_payment = SimpleNamespace(
        invoice_payload="month:77", total_amount=149000,
        telegram_payment_charge_id="tg_1", provider_payment_charge_id="yk_1",
    )
    await B.on_successful_payment(m)
    assert database.is_trial_active(uid, db_path=db) is True
    first_until = database.get_user(uid, db_path=db)["access_until"]
    assert send.call_args.args[0] == uid
    assert "Оплата прошла" in send.call_args.args[1]

    # Повторная доставка того же платежа не продлевает доступ ещё раз
    send.reset_mock()
    m2 = _msg(uid)
    m2.successful_payment = m.successful_payment
    await B.on_successful_payment(m2)
    assert database.get_user(uid, db_path=db)["access_until"] == first_until
    send.assert_not_called()


def test_extend_access_adds_to_active_period(tmp_path):
    from datetime import datetime
    p = str(tmp_path / "e.db")
    database.add_user(1, "x", db_path=p)
    u1 = datetime.fromisoformat(database.extend_access(1, 30, db_path=p))
    u2 = datetime.fromisoformat(database.extend_access(1, 30, db_path=p))
    assert 59 <= (u2 - datetime.utcnow()).days <= 60
    assert u2 > u1
    assert database.extend_access(999, 30, db_path=p) is None


@pytest.mark.asyncio
async def test_buy_keyboard_uses_invoice_when_token(monkeypatch):
    monkeypatch.setattr(B, "PAYMENT_PROVIDER_TOKEN", "tok")
    kb = B.get_buy_keyboard()
    assert kb.inline_keyboard[0][0].callback_data == "buy_month"
    monkeypatch.setattr(B, "PAYMENT_PROVIDER_TOKEN", "")
    kb = B.get_buy_keyboard()
    assert getattr(kb.inline_keyboard[0][0], "url", None)


@pytest.mark.asyncio
async def test_stats(db):
    m = _msg(90)
    await B.cmd_stats(m)
    assert "Пробный период до" in m.answer.call_args.args[0]


# ─── ЮMoney ─────────────────────────────────────────────────────────────────

import yoomoney_pay as ym  # noqa: E402


def test_ym_label_roundtrip():
    label = ym.make_label("forever", 123)
    assert len(label) <= 64
    assert ym.parse_label(label) == ("forever", 123)
    assert ym.parse_label("чужая метка") is None


def test_ym_match_payments():
    pending = [
        {"label": "wf:month:1:aa", "user_id": 1, "plan": "month"},
        {"label": "wf:forever:2:bb", "user_id": 2, "plan": "forever"},
        {"label": "wf:month:3:cc", "user_id": 3, "plan": "month"},
    ]
    ops = [
        {"operation_id": "op1", "label": "wf:month:1:aa", "status": "success", "direction": "in", "amount": 1445.30},
        {"operation_id": "op2", "label": "wf:forever:2:bb", "status": "in_progress", "direction": "in", "amount": 4850},
        {"operation_id": "op3", "label": "wf:month:3:cc", "status": "success", "direction": "in", "amount": 100},
        {"operation_id": "op4", "label": "other", "status": "success", "direction": "in", "amount": 9999},
    ]
    found = ym.match_payments(ops, pending, {"month": 1490, "forever": 5000})
    assert [f["operation_id"] for f in found] == ["op1"]  # в процессе и недоплата не засчитываются


def test_ym_keyboard_when_enabled(monkeypatch):
    monkeypatch.setenv("YOOMONEY_WALLET", "4100111")
    monkeypatch.setenv("YOOMONEY_TOKEN", "tok")
    kb = B.get_buy_keyboard()
    assert kb.inline_keyboard[0][0].callback_data == "ym_month"


@pytest.mark.asyncio
async def test_ym_full_flow(db, monkeypatch):
    monkeypatch.setenv("YOOMONEY_WALLET", "4100111")
    monkeypatch.setenv("YOOMONEY_TOKEN", "tok")
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)

    async def fake_url(label, amount, title, payment_type="AC"):
        return "https://yoomoney.ru/transfer/quickpay?requestId=x"
    monkeypatch.setattr(ym, "create_payment_url", fake_url)

    uid = 555
    c = _cb(uid, "ym_forever")
    await B.callback_ym_buy(c)
    pending = database.get_pending_payments(user_id=uid, db_path=db)
    assert len(pending) == 1 and pending[0]["plan"] == "forever"
    label = pending[0]["label"]
    kb = c.message.answer.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].url.startswith("https://yoomoney.ru/")

    # Пока платежа нет — «не найден»
    async def no_ops(since, records=100):
        return []
    monkeypatch.setattr(ym, "fetch_incoming", no_ops)
    c2 = _cb(uid, "ym_check")
    await B.callback_ym_check(c2)
    assert "пока не найден" in c2.message.answer.call_args.args[0]

    # Платёж пришёл — доступ навсегда
    async def ops(since, records=100):
        return [{"operation_id": "777", "label": label, "status": "success", "direction": "in", "amount": 4850.0}]
    monkeypatch.setattr(ym, "fetch_incoming", ops)
    assert await B._check_yoomoney() == 1
    assert database.is_trial_active(uid, db_path=db)
    assert "навсегда" in send.call_args.args[1]
    assert database.get_pending_payments(user_id=uid, db_path=db) == []

    # Повторная проверка не засчитывает второй раз
    assert await B._check_yoomoney() == 0


def test_ym_quickpay_params(monkeypatch):
    monkeypatch.setenv("YOOMONEY_WALLET", "4100111")
    p = ym.quickpay_params("wf:month:1:aa", 1490, "WildFinance — 1 месяц")
    assert p["receiver"] == "4100111" and p["sum"] == "1490.00"
    assert p["quickpay-form"] == "button" and p["label"] == "wf:month:1:aa"
