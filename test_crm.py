"""Тесты CRM: события воронки и API для Obsidian."""

import json
import os

import pytest
from unittest.mock import AsyncMock

import bot as B
import crm
import database
from test_bot_features import db, _msg, _cb  # noqa: F401


def test_authorized(monkeypatch):
    monkeypatch.setenv("CRM_TOKEN", "x" * 32)
    assert crm.enabled()
    assert crm.authorized("Bearer " + "x" * 32, None)
    assert crm.authorized(None, "x" * 32)
    assert not crm.authorized("Bearer wrong", None)
    assert not crm.authorized(None, None)
    monkeypatch.setenv("CRM_TOKEN", "short")
    assert not crm.enabled() and not crm.authorized("Bearer short", None)


def test_payload(tmp_path):
    p = str(tmp_path / "c.db")
    database.add_user(1, "Anna_Seller", db_path=p)
    database.set_user_source(1, "u_anna_seller", db_path=p)
    database.log_event(1, "start:u_anna_seller", db_path=p)
    database.log_event(1, "demo", db_path=p)
    database.log_event(1, "demo", db_path=p)
    database.log_event(1, "report", db_path=p)
    database.record_payment("c1", 1, "month", 149000, db_path=p)
    database.add_user(2, None, db_path=p)

    data = crm.build_payload(p)
    json.dumps(data)                                  # сериализуется
    users = {u["user_id"]: u for u in data["users"]}
    u = users[1]
    assert u["username"] == "Anna_Seller" and u["source"] == "u_anna_seller"
    assert u["events"]["demo"]["count"] == 2
    assert "start:u_anna_seller" in u["events"]
    assert u["payments"] == 1 and u["paid_rub"] == 1490
    assert users[2]["username"] == "" and users[2]["events"] == {}


def test_delete_me_removes_events(tmp_path):
    p = str(tmp_path / "d.db")
    database.add_user(5, "x", db_path=p)
    database.log_event(5, "demo", db_path=p)
    database.delete_user_data(5, db_path=p)
    assert crm.build_payload(p)["users"][0]["events"] == {}


@pytest.mark.asyncio
async def test_bot_logs_funnel_events(db):
    await B.cmd_start(_msg(31, text="/start u_lead_31"))
    await B.callback_demo(_cb(31, "demo_WB"))
    await B.cmd_buy(_msg(31, text="/buy"))
    ev = {u["user_id"]: u for u in crm.build_payload(db)["users"]}[31]["events"]
    assert {"start:u_lead_31", "demo", "buy_open"} <= set(ev)


def test_long_username_source_kept():
    name = "u_" + "a" * 32
    assert B._parse_start_source(f"/start {name}") == name


# ─── Активность и /users ────────────────────────────────────────────────────

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def test_touch_user_counts_visits(tmp_path):
    p = str(tmp_path / "a.db")
    database.touch_user(1, "ghost", db_path=p)        # нет в базе — ничего не создаём
    assert database.get_crm_rows(p) == []
    database.add_user(1, "seller", db_path=p)
    t0 = datetime(2026, 10, 1, 10, 0)
    database.touch_user(1, "seller", db_path=p, now=t0)
    database.touch_user(1, "seller", db_path=p, now=t0 + timedelta(minutes=5))    # тот же заход
    database.touch_user(1, "seller", db_path=p, now=t0 + timedelta(hours=3))      # новый заход
    u = database.get_crm_rows(p)[0]
    assert (u["visits"], u["actions"]) == (2, 3)
    assert u["last_seen"] == "2026-10-01T13:00:00"


@pytest.mark.asyncio
async def test_middleware_touches_after_handler(db):
    database.add_user(70, "x", db_path=db)
    calls = []

    async def handler(event, data):
        calls.append("handler")
        return "ok"

    ev = SimpleNamespace(from_user=SimpleNamespace(id=70, username="x"))
    assert await B._activity_middleware(handler, ev, {}) == "ok"
    assert calls == ["handler"]
    assert database.get_crm_rows(db)[0]["actions"] == 1


@pytest.mark.asyncio
async def test_users_command_shows_demo_and_activity(db, monkeypatch):
    monkeypatch.setattr(B, "ADMIN_ID", 1)
    database.add_user(80, "watcher", db_path=db)
    database.add_user(81, "silent", db_path=db)
    database.set_user_source(80, "ave", db_path=db)
    database.log_event(80, "demo", db_path=db)
    database.touch_user(80, "watcher", db_path=db)
    m = _msg(1, "/users")
    await B.cmd_users(m)
    text = "\n".join(c.args[0] for c in m.answer.call_args_list)
    assert "Смотрели пример: 1" in text
    card = text[text.index("@watcher"):text.index("@silent")]   # активный — первым
    assert "Пример: да, 1 раз" in card and "метка <code>ave</code>" in card
    assert "Был:" in card and "Заходов: 1" in card
    assert "Пример: не открывал" in text[text.index("@silent"):]


@pytest.mark.asyncio
async def test_users_command_admin_only(db):
    m = _msg(5, "/users")
    await B.cmd_users(m)
    assert "нет доступа" in m.answer.call_args.args[0]


def test_ago_and_msk():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=B.timezone.utc)
    assert B._ago("2026-10-01T11:50:00", now) == "10 мин назад"
    assert B._ago("2026-09-29T12:00:00", now) == "2 дн назад"
    assert B._msk("2026-10-01T17:15:00") == "01.10 20:15"
    assert B._msk(None) == "—"


@pytest.mark.asyncio
async def test_users_shows_reports_by_marketplace(db, monkeypatch):
    monkeypatch.setattr(B, "ADMIN_ID", 1)
    database.add_user(90, "mixed", db_path=db)
    for _ in range(3):
        database.increment_report_count(90, db_path=db)
    database.log_event(90, "report:WB", db_path=db)
    database.log_event(90, "report:Ozon", db_path=db)
    database.save_last_report(90, {"marketplace": "Ozon", "items": []}, "o.csv", db_path=db)
    m = _msg(1, "/users")
    await B.cmd_users(m)
    text = "\n".join(c.args[0] for c in m.answer.call_args_list)
    assert "Отчёты по площадкам: Ozon: 1 · WB: 1 · до учёта: 1" in text
    assert "Отчётов: 3 (Ozon — 1, WB — 1, до учёта — 1) · последний Ozon" in text


@pytest.mark.asyncio
async def test_report_event_has_marketplace(db, monkeypatch):
    from test_bot_features import _send_report
    monkeypatch.setattr(B.asyncio, "sleep", AsyncMock())
    await _send_report(91, monkeypatch)
    ev = {u["user_id"]: u for u in database.get_crm_rows(db)}[91]["events"]
    assert "report:Ozon" in ev


@pytest.mark.asyncio
async def test_demo_reupload_is_not_counted(db, monkeypatch):
    import conftest
    from test_bot_features import _send_report
    monkeypatch.setattr(B, "_demo_marketplace_of", conftest.REAL_DEMO_CHECK)
    calls = await _send_report(92, monkeypatch)      # шлёт ozon_real_report.xlsx = наш пример Ozon
    texts = [c.args[0] for c in calls]
    assert "файл-пример" in texts[1]
    assert any("К ВЫПЛАТЕ" in t for t in texts)
    u = {r["user_id"]: r for r in database.get_crm_rows(db)}[92]
    assert u["reports"] == 0 and "demo_reupload" in u["events"]
    assert database.get_last_report(92, db_path=db) is None


def test_demo_check_matches_only_examples(tmp_path):
    import conftest
    import shutil
    copy = tmp_path / "любое_имя.xlsx"
    shutil.copy(B.DEMO_FILES["WB"], copy)
    assert conftest.REAL_DEMO_CHECK(str(copy)) == "WB"
    other = tmp_path / "x.xlsx"
    other.write_bytes(b"not a demo")
    assert conftest.REAL_DEMO_CHECK(str(other)) is None
