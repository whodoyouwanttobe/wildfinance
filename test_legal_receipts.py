"""Тесты: оферта/политика, чеки «Мой налог», удаление данных."""

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import bot as B  # noqa: E402
import database
import legal
import nalog_receipts as NR
from test_bot_features import db, _msg, _cb  # noqa: F401  (фикстура и хелперы)


SELLER = {
    "SELLER_NAME": "Иванов Иван Иванович",
    "SELLER_INN": "123456789012",
    "SELLER_EMAIL": "help@example.com",
}


@pytest.fixture
def seller_env(monkeypatch, tmp_path):
    for k, v in SELLER.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OFFER_URL", raising=False)
    monkeypatch.delenv("PRIVACY_URL", raising=False)
    return tmp_path


# ─── Документы ──────────────────────────────────────────────────────────────

def test_render_fills_all_placeholders(seller_env):
    for kind in ("offer", "privacy"):
        text = legal.render(kind)
        assert "{{" not in text
        assert "Иванов Иван Иванович" in text and "123456789012" in text
        assert "help@example.com" in text


def test_render_marks_missing_values(monkeypatch):
    monkeypatch.delenv("SELLER_NAME", raising=False)
    monkeypatch.delenv("SELLER_INN", raising=False)
    monkeypatch.delenv("SELLER_EMAIL", raising=False)
    assert not legal.configured()
    assert "[seller_inn]" in legal.render("offer")


def test_configured_checks_inn(monkeypatch, seller_env):
    assert legal.configured()
    monkeypatch.setenv("SELLER_INN", "12345")
    assert not legal.configured()


def test_offer_key_terms(seller_env):
    text = legal.render("offer")
    for phrase in ("акцепт", "Навсегда", "Автоматического продления", "3 календарных дней",
                   "Мой налог", "справочный характер", "/privacy"):
        assert phrase in text, phrase


def test_privacy_key_terms(seller_env):
    text = legal.render("privacy")
    for phrase in ("152-ФЗ", "Telegram ID", "/delete_me", "территории Российской Федерации",
                   "10 рабочих дней", "удаляются сразу после обработки"):
        assert phrase in text, phrase


def test_telegram_chunks_are_valid_and_short(seller_env):
    chunks = legal.to_telegram_chunks(legal.render("offer"), limit=1500)
    assert len(chunks) > 1
    assert all(len(c) <= 1500 for c in chunks)
    joined = "\n".join(chunks)
    assert "**" not in joined and "<b>Исполнитель</b>" in joined
    assert joined.count("<b>") == joined.count("</b>")


def test_telegraph_nodes_structure(seller_env):
    nodes = legal.to_telegraph_nodes(legal.render("privacy"))
    tags = {n["tag"] for n in nodes}
    assert {"h3", "p", "ul"} <= tags
    assert all(isinstance(c, (str, dict)) for n in nodes for c in n["children"])
    assert legal.title("privacy").startswith("Политика обработки")
    assert len(json.dumps(nodes, ensure_ascii=False).encode()) < 64_000


def test_links_html_with_and_without_urls(seller_env, monkeypatch):
    assert "/terms" in legal.links_html() and "/privacy" in legal.links_html()
    monkeypatch.setenv("OFFER_URL", "https://telegra.ph/offer")
    html_ = legal.links_html()
    assert 'href="https://telegra.ph/offer"' in html_ and "/privacy" in html_


def test_ensure_published_creates_then_edits(seller_env, monkeypatch):
    calls = []

    async def fake_tg(client, method, payload):
        calls.append(method)
        if method == "createAccount":
            return {"access_token": "tok"}
        path = method.split("/", 1)[1] if "/" in method else f"page-{len(calls)}"
        return {"path": path, "url": f"https://telegra.ph/{path}"}

    monkeypatch.setattr(legal, "_telegraph", fake_tg)

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: DummyClient())

    urls = asyncio.run(legal.ensure_published())
    assert set(urls) == {"offer", "privacy"}
    assert calls == ["createAccount", "createPage", "createPage"]
    assert legal.url("offer") == urls["offer"]

    calls.clear()
    asyncio.run(legal.ensure_published())       # ничего не поменялось
    assert calls == []

    monkeypatch.setenv("SELLER_EMAIL", "new@example.com")
    asyncio.run(legal.ensure_published())       # реквизиты поменялись → правим те же страницы
    assert calls and all(c.startswith("editPage/") for c in calls)


def test_ensure_published_skips_without_seller(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SELLER_NAME", raising=False)
    assert asyncio.run(legal.ensure_published()) == {}


@pytest.mark.asyncio
async def test_cmd_terms_falls_back_to_text(seller_env):
    m = _msg(1, "/terms")
    await B.cmd_terms(m)
    texts = [c.args[0] for c in m.answer.call_args_list]
    assert len(texts) > 1 and "Публичная оферта" in texts[0]


@pytest.mark.asyncio
async def test_cmd_privacy_sends_link_button(seller_env, monkeypatch):
    monkeypatch.setenv("PRIVACY_URL", "https://telegra.ph/privacy")
    m = _msg(1, "/privacy")
    await B.cmd_privacy(m)
    assert m.answer.call_count == 1
    kb = m.answer.call_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].url == "https://telegra.ph/privacy"


@pytest.mark.asyncio
async def test_buy_mentions_offer(seller_env):
    m = _msg(1, "/buy")
    await B.cmd_buy(m)
    assert "публичной оферты" in m.answer.call_args.args[0]


# ─── Чеки ───────────────────────────────────────────────────────────────────

def test_income_payload():
    p = NR.build_income_payload("month", 1490)
    assert p["totalAmount"] == "1490.00"
    assert p["services"][0]["amount"] == 1490.0 and p["services"][0]["quantity"] == 1
    assert "1 месяц" in p["services"][0]["name"]
    assert p["client"]["incomeType"] == "FROM_INDIVIDUAL"
    assert p["operationTime"].endswith("+03:00")


def test_receipt_url_check():
    good = "https://lknpd.nalog.ru/api/v1/receipt/123456789012/200abcde12/print"
    assert NR.looks_like_receipt_url(good)
    assert NR.receipt_url("123456789012", "200abcde12") == good
    assert not NR.looks_like_receipt_url("https://evil.example.com/receipt")


def test_auto_enabled(monkeypatch):
    monkeypatch.delenv("NALOG_INN", raising=False)
    monkeypatch.delenv("NALOG_PASSWORD", raising=False)
    assert not NR.auto_enabled()
    monkeypatch.setenv("NALOG_INN", "123456789012")
    monkeypatch.setenv("NALOG_PASSWORD", "x")
    assert NR.auto_enabled()


def test_receipts_db(tmp_path):
    p = str(tmp_path / "r.db")
    n = database.add_receipt("c1", 5, "month", 149000, db_path=p)
    assert database.add_receipt("c1", 5, "month", 149000, db_path=p) == n   # повтор — тот же номер
    assert [r["id"] for r in database.get_pending_receipts(db_path=p)] == [n]
    row = database.set_receipt_url(n, "https://lknpd.nalog.ru/x", db_path=p)
    assert row["user_id"] == 5 and row["url"]
    assert database.get_pending_receipts(db_path=p) == []
    assert database.get_user_receipts(5, db_path=p)[0]["url"] == "https://lknpd.nalog.ru/x"
    assert database.set_receipt_url(999, "u", db_path=p) is None


def _pay(uid, charge="tg_r1", payload="month:1"):
    m = _msg(uid)
    m.successful_payment = SimpleNamespace(
        invoice_payload=payload, total_amount=149000,
        telegram_payment_charge_id=charge, provider_payment_charge_id="yk",
    )
    return m


@pytest.mark.asyncio
async def test_manual_receipt_flow(db, monkeypatch):
    monkeypatch.delenv("NALOG_INN", raising=False)
    monkeypatch.setattr(NR, "_client", None)
    monkeypatch.setattr(B, "ADMIN_ID", 999)
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)

    await B.on_successful_payment(_pay(10))
    texts = [c.args[1] for c in send.call_args_list]
    user_msg = next(t for c, t in zip(send.call_args_list, texts) if c.args[0] == 10)
    assert "в течение дня" in user_msg
    admin = [t for c, t in zip(send.call_args_list, texts) if c.args[0] == 999]
    assert any("вручную" in t for t in admin)
    assert any("/receipt 1" in t and "1490.00" in t for t in admin)

    # Пользователь видит «чек формируется»
    m = _msg(10, "/receipts")
    await B.cmd_receipts(m)
    assert "формируется" in m.answer.call_args.args[0]

    # Не-админ не может выдать чек
    send.reset_mock()
    m = _msg(10, "/receipt 1 https://lknpd.nalog.ru/api/v1/receipt/123456789012/abc/print")
    await B.cmd_receipt(m)
    send.assert_not_called()

    # Админ: очередь и выдача
    m = _msg(999, "/receipts")
    await B.cmd_receipts(m)
    assert "Ждут ручного чека" in m.answer.call_args.args[0]

    m = _msg(999, "/receipt 1 https://example.com/x")
    await B.cmd_receipt(m)
    assert "не похоже" in m.answer.call_args.args[0]

    link = "https://lknpd.nalog.ru/api/v1/receipt/123456789012/abc/print"
    m = _msg(999, f"/receipt 1 {link}")
    await B.cmd_receipt(m)
    assert "отправлен" in m.answer.call_args.args[0]
    assert send.call_args.args[0] == 10
    assert send.call_args.kwargs["reply_markup"].inline_keyboard[0][0].url == link

    m = _msg(10, "/receipts")
    await B.cmd_receipts(m)
    assert link in m.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_auto_receipt_flow(db, monkeypatch):
    link = "https://lknpd.nalog.ru/api/v1/receipt/123456789012/uuid1/print"
    client = MagicMock()
    client.create_receipt = AsyncMock(return_value=("uuid1", link))
    monkeypatch.setattr(NR, "get_client", lambda: client)
    monkeypatch.setattr(B, "ADMIN_ID", 0)
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)

    await B.on_successful_payment(_pay(11, charge="tg_auto"))
    client.create_receipt.assert_awaited_once_with("month", 1490.0)
    first, last = send.call_args_list[0], send.call_args_list[-1]
    assert "Оплата прошла" in first.args[1] and "следующем сообщении" in first.args[1]
    assert last.kwargs["reply_markup"].inline_keyboard[0][0].url == link


@pytest.mark.asyncio
async def test_auto_receipt_failure_falls_back(db, monkeypatch):
    client = MagicMock()
    client.create_receipt = AsyncMock(side_effect=RuntimeError("ФНС недоступна"))
    monkeypatch.setattr(NR, "get_client", lambda: client)
    monkeypatch.setattr(B, "ADMIN_ID", 999)
    send = AsyncMock()
    monkeypatch.setattr(B.bot, "send_message", send, raising=False)

    await B.on_successful_payment(_pay(12, charge="tg_fail"))
    texts = [c.args[1] for c in send.call_args_list]
    assert any("в течение дня" in t for t in texts)
    assert any("Нужен чек" in t for t in texts)


# ─── Удаление данных ────────────────────────────────────────────────────────

def test_delete_user_data(tmp_path):
    p = str(tmp_path / "d.db")
    database.add_user(7, "seller", db_path=p)
    database.set_costs(7, "WB", {"A1": 100.0}, db_path=p)
    database.set_tax(7, "usn6", 6, db_path=p)
    database.save_last_report(7, {"marketplace": "WB"}, "f.xlsx", db_path=p)
    database.record_payment("c7", 7, "month", 149000, db_path=p)
    database.add_receipt("c7", 7, "month", 149000, db_path=p)

    database.delete_user_data(7, db_path=p)
    assert database.get_costs(7, db_path=p) == {}
    assert database.get_tax(7, db_path=p)[0] is None
    assert database.get_last_report(7, db_path=p) is None
    assert database.get_user(7, db_path=p)["username"] is None
    assert database.get_payments_total(db_path=p)[0] == 1
    assert len(database.get_user_receipts(7, db_path=p)) == 1


@pytest.mark.asyncio
async def test_delete_me_flow(db):
    m = _msg(20, "/delete_me")
    await B.cmd_delete_me(m)
    assert "Удалить мои данные" in m.answer.call_args.args[0]

    c = _cb(20, "delete_me_no")
    await B.callback_delete_me(c)
    assert "ничего не удаляю" in c.message.edit_text.call_args.args[0]

    B._cost_state[20] = {"mode": "list"}
    c = _cb(20, "delete_me_yes")
    await B.callback_delete_me(c)
    assert "удалены" in c.message.edit_text.call_args.args[0]
    assert 20 not in B._cost_state
