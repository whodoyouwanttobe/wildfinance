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
                 "mark_pending_paid", "set_costs", "get_costs", "delete_cost", "get_tax",
                 "set_tax", "save_last_report", "get_last_report", "set_user_source",
                 "add_receipt", "set_receipt_url", "get_user_receipts", "get_pending_receipts",
                 "delete_user_data"):
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
async def test_demo_sends_report_then_file(mp):
    c = _cb(data=f"demo_{mp}")
    await B.callback_demo(c)
    # Сначала разбор (HTML), потом файл
    texts = [call.args[0] for call in c.message.answer.call_args_list]
    report = "\n".join(t for t in texts if "ПРИМЕР РАЗБОРА" in t or "К ВЫПЛАТЕ" in t)
    assert f"ПРИМЕР РАЗБОРА — {mp}" in report
    assert "К ВЫПЛАТЕ" in report
    kb = c.message.answer.call_args_list[-1].kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    other = "Ozon" if mp == "WB" else "WB"
    assert datas == [f"demoai_{mp}", f"demo_{other}"]
    c.message.answer_document.assert_called_once()
    sent = c.message.answer_document.call_args.args[0]
    assert os.path.exists(sent.path)
    assert B.detect_marketplace(sent.path) == mp
    assert "свой отчёт" in c.message.answer_document.call_args.kwargs["caption"]


@pytest.mark.asyncio
async def test_demo_does_not_touch_user_report_or_counter(db):
    B._last_reports[5] = {"text": "мой отчёт", "marketplace": "WB"}
    c = _cb(5, data="demo_WB")
    await B.callback_demo(c)
    assert B._last_reports[5]["text"] == "мой отчёт"
    assert B.get_user(5) is None or B.get_user(5).get("report_count", 0) == 0


@pytest.mark.asyncio
async def test_demo_ai_is_cached(monkeypatch):
    B._demo_ai_cache.clear()
    calls = []

    async def fake_ai(report_text, marketplace):
        calls.append(marketplace)
        return "Вывод <1>"

    monkeypatch.setattr(B, "ai_summarize_report", fake_ai)
    monkeypatch.setattr(B, "get_ai_client", lambda: SimpleNamespace(available=True))
    c = _cb(data="demoai_Ozon")
    await B.callback_demo_ai(c)
    thinking = c.message.answer.return_value
    assert "Вывод &lt;1&gt;" in thinking.edit_text.call_args.args[0]
    c2 = _cb(data="demoai_Ozon")
    await B.callback_demo_ai(c2)
    assert calls == ["Ozon"]
    assert "Вывод" in c2.message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_demo_ai_unavailable(monkeypatch):
    B._demo_ai_cache.clear()
    monkeypatch.setattr(B, "get_ai_client", lambda: SimpleNamespace(available=False))
    c = _cb(data="demoai_WB")
    await B.callback_demo_ai(c)
    assert "недоступен" in c.message.answer.call_args.args[0]


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
async def test_compare_mixed_marketplaces_shows_wb_vs_ozon(db, monkeypatch):
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
    out = "\n".join(c.args[0] for c in m2.answer.call_args_list)
    assert "разных площадок" in out and "WB против OZON" in out
    assert uid not in B._compare_state


@pytest.mark.asyncio
async def test_wbozon_flow_any_order(db, monkeypatch):
    uid = 45
    await B.cmd_wbozon(_msg(uid, text="/wbozon"))
    files = iter([os.path.join(HERE, "ozon_real_report.xlsx"), os.path.join(HERE, "ozon_real_report.xlsx"),
                  os.path.join(HERE, "wb_real_report.xlsx")])

    async def fake_download(document, destination):
        import shutil
        shutil.copy(next(files), destination)

    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    doc = SimpleNamespace(file_name="x.xlsx", file_size=1000)
    m1 = _msg(uid, document=doc)
    await B.handle_document(m1)
    assert "пришли отчёт <b>WB</b>" in m1.answer.call_args.args[0]
    m2 = _msg(uid, document=doc)       # Ozon ещё раз — просто заменяем
    await B.handle_document(m2)
    assert "WB" in m2.answer.call_args.args[0]
    m3 = _msg(uid, document=doc)
    await B.handle_document(m3)
    assert "WB против OZON" in m3.answer.call_args.args[0]
    assert uid not in B._compare_state


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


# ─── Акция «навсегда» до 11 октября, тариф на 12 месяцев после ─────────────

from datetime import datetime as _dt  # noqa: E402

_BEFORE = _dt(2026, 9, 30, 23, 41, tzinfo=B.MSK)
_LAST_DAY = _dt(2026, 10, 11, 23, 59, tzinfo=B.MSK)
_AFTER = _dt(2026, 10, 12, 0, 1, tzinfo=B.MSK)


def test_promo_window(monkeypatch):
    monkeypatch.setattr(B, "FOREVER_PROMO_UNTIL", "2026-10-11")
    assert B.forever_available(_BEFORE) and B.forever_available(_LAST_DAY)
    assert not B.forever_available(_AFTER)
    assert B.promo_days_left(_BEFORE) == 11
    assert "осталось 11 дней" in B.promo_left_text(_BEFORE)
    assert "последний день" in B.promo_left_text(_LAST_DAY)
    assert B.available_plans(_BEFORE) == ["month", "forever"]
    assert B.available_plans(_AFTER) == ["month", "year"]


def test_days_word():
    assert [B.days_word(n) for n in (1, 2, 5, 11, 21, 22, 25)] == \
        ["день", "дня", "дней", "дней", "день", "дня", "дней"]


def test_tariffs_text_switches_after_promo(monkeypatch):
    monkeypatch.setattr(B, "FOREVER_PROMO_UNTIL", "2026-10-11")
    before = B.tariffs_text(_BEFORE)
    after = B.tariffs_text(_AFTER)
    assert "Навсегда" in before and str(B.PLANS["forever"]["price_rub"]) in before
    assert "до 11 октября" in before
    assert "12 месяцев" in after and "Навсегда" not in after


def test_buy_keyboard_after_promo(monkeypatch):
    monkeypatch.setattr(B, "FOREVER_PROMO_UNTIL", "2026-10-11")
    monkeypatch.setenv("YOOMONEY_WALLET", "4100111")
    monkeypatch.setenv("YOOMONEY_TOKEN", "tok")
    kb_before = B.get_buy_keyboard(_BEFORE)
    kb_after = B.get_buy_keyboard(_AFTER)
    assert [r[0].callback_data for r in kb_before.inline_keyboard] == ["ym_month", "ym_forever"]
    assert "ещё 11 дней" in kb_before.inline_keyboard[1][0].text
    assert [r[0].callback_data for r in kb_after.inline_keyboard] == ["ym_month", "ym_year"]


@pytest.mark.asyncio
async def test_stale_forever_button_after_promo(db, monkeypatch):
    monkeypatch.setattr(B, "FOREVER_PROMO_UNTIL", "2026-10-11")
    monkeypatch.setattr(B, "_now_msk", lambda: _AFTER)
    monkeypatch.setenv("YOOMONEY_WALLET", "4100111")
    monkeypatch.setenv("YOOMONEY_TOKEN", "tok")
    c = _cb(600, "ym_forever")
    await B.callback_ym_buy(c)
    assert "акция уже закончилась" in c.message.answer.call_args.args[0]
    assert database.get_pending_payments(user_id=600, db_path=db) == []


# ─── Отчёт: кнопки под отчётом, реклама только для неоплативших ────────────

async def _send_report(uid, monkeypatch):
    async def fake_download(document, destination):
        import shutil
        shutil.copy(os.path.join(HERE, "ozon_real_report.xlsx"), destination)
    monkeypatch.setattr(B.bot, "download", fake_download, raising=False)
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    B._compare_state.pop(uid, None)
    m = _msg(uid, document=SimpleNamespace(file_name="r.xlsx", file_size=1000))
    await B.handle_document(m)
    return [c for c in m.answer.call_args_list]


@pytest.mark.asyncio
async def test_report_trial_user_sees_promo_and_inline_buttons(db, monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    calls = await _send_report(700, monkeypatch)
    texts = [c.args[0] for c in calls]
    assert not any(t == "Что дальше?" for t in texts)
    report_call = next(c for c in calls if "ОТЧЁТ OZON" in c.args[0] or "/buy" in c.args[0])
    full = "\n".join(t for t in texts)
    assert "/buy" in full and "Пробный период" in full
    last_report = [c for c in calls if c.kwargs.get("reply_markup") is not None][0]
    kb = last_report.kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].callback_data == "cost_menu" and kb[1][0].callback_data == "post_review"
    assert report_call is not None


@pytest.mark.asyncio
async def test_report_paid_user_no_promo(db, monkeypatch):
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    database.add_user(701, "paid", db_path=db)
    database.extend_access(701, 30, db_path=db)
    calls = await _send_report(701, monkeypatch)
    full = "\n".join(c.args[0] for c in calls)
    assert "ОТЧЁТ OZON" in full
    assert "/buy" not in full and "Пробный период" not in full


@pytest.mark.asyncio
async def test_drop_only_pressed_button():
    c = _cb(1, "post_ai")
    c.message.reply_markup = B.get_post_report_keyboard()
    await B._drop_pressed_button(c)
    kb = c.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "post_ai" not in datas and "post_review" in datas


# ─── Источники трафика ──────────────────────────────────────────────────────

def test_parse_start_source():
    assert B._parse_start_source("/start wbchat") == "wbchat"
    assert B._parse_start_source("/start ref_123") == "ref_123"
    assert B._parse_start_source("/start") == ""
    assert B._parse_start_source("/start <script>") == ""


@pytest.mark.asyncio
async def test_start_saves_source_and_stats(db, monkeypatch):
    monkeypatch.setattr(B, "set_user_source", lambda uid, src: database.set_user_source(uid, src, db_path=db))
    m = _msg(800, text="/start wbchat")
    await B.cmd_start(m)
    m2 = _msg(800, text="/start other")  # повторный старт не перезаписывает источник
    await B.cmd_start(m2)
    await B.cmd_start(_msg(801, text="/start"))
    database.record_payment("c1", 800, "month", 149000, db_path=db)
    stats = {s["source"]: s for s in database.get_source_stats(db_path=db)}
    assert stats["wbchat"]["users"] == 1 and stats["wbchat"]["payers"] == 1
    assert stats["wbchat"]["revenue_kop"] == 149000
    assert stats["(без метки)"]["users"] == 1
