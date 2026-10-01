"""Общие настройки тестов: ни один тест не пишет в настоящую users.db."""
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import database  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "isolated.db"))
