"""Общие настройки тестов: ни один тест не пишет в настоящую users.db."""
import os

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")

import database  # noqa: E402
import bot  # noqa: E402

# Тесты отправляют боту файлы-примеры как «свои» отчёты. Настоящая проверка
# «это наш пример?» отключена по умолчанию; test_demo_reupload включает её сам.
REAL_DEMO_CHECK = bot._demo_marketplace_of


@pytest.fixture(autouse=True)
def _isolated_db_path(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "isolated.db"))
    monkeypatch.setattr(bot, "_demo_marketplace_of", lambda path: None)
