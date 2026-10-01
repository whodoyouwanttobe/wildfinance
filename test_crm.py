"""Тесты CRM: события воронки и API для Obsidian."""

import json
import os

import pytest

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
