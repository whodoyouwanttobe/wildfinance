"""
test_all.py — Тесты для wb_parser, database и bot.

Запуск:
  python -m pytest test_all.py -v
"""

import os
import sys
import csv
import sqlite3
import tempfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pandas as pd

# Добавляем путь к solution в sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiogram.types import Message

from wb_parser import analyze, load_report, detect_column_mapping, safe_col, fmt
from database import (
    add_user, is_trial_active, get_user, get_connection,
    grant_access, increment_report_count, save_feedback, has_given_feedback,
)


# ═══════════════════════════════════════════════════════════════════
# Фикстуры
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def test_csv(tmp_path):
    """Создаёт тестовый CSV-файл с 8 строками из задания."""
    filepath = tmp_path / "test_report.csv"
    rows = [
        {
            "Артикул поставщика": "sku_1",
            "Обоснование для оплаты": "Продажа",
            "К перечислению Продавцу за реализованный товар": 1000,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "sku_1",
            "Обоснование для оплаты": "Логистика",
            "К перечислению Продавцу за реализованный товар": 0,
            "Услуги по доставке товара покупателю": 100,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "sku_1",
            "Обоснование для оплаты": "Продажа",
            "К перечислению Продавцу за реализованный товар": 1000,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "sku_2",
            "Обоснование для оплаты": "Продажа",
            "К перечислению Продавцу за реализованный товар": 2000,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "sku_2",
            "Обоснование для оплаты": "Возврат",
            "К перечислению Продавцу за реализованный товар": 2000,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "sku_2",
            "Обоснование для оплаты": "Штраф",
            "К перечислению Продавцу за реализованный товар": 0,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 1000,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "",
            "Обоснование для оплаты": "Хранение",
            "К перечислению Продавцу за реализованный товар": 0,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 300,
            "Операции при приёмке": 0,
            "Удержания": 0,
        },
        {
            "Артикул поставщика": "",
            "Обоснование для оплаты": "Удержания",
            "К перечислению Продавцу за реализованный товар": 0,
            "Услуги по доставке товара покупателю": 0,
            "Общая сумма штрафов": 0,
            "Хранение": 0,
            "Операции при приёмке": 0,
            "Удержания": 200,
        },
    ]
    df = pd.DataFrame(rows)
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    return str(filepath)


@pytest.fixture
def test_db(tmp_path):
    """Создаёт путь к временной тестовой БД."""
    return str(tmp_path / "test_users.db")


# ═══════════════════════════════════════════════════════════════════
# Тесты wb_parser.py
# ═══════════════════════════════════════════════════════════════════

class TestWbParser:
    """Тесты парсера финансовых отчётов."""

    def test_analyze_returns_string(self, test_csv):
        """analyze() возвращает строку, а не None."""
        result = analyze(test_csv)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_total_profit(self, test_csv):
        """Чистая прибыль = (4000 продаж − 2000 возврат) − 1600 расходов = 400.
        По правилам WB сумма возврата вычитается из дохода продавца."""
        result = analyze(test_csv)
        assert "К ВЫПЛАТЕ ОТ WB:    400.00" in result

    def test_sku1_profit(self, test_csv):
        """sku_1: доход 2000 - логистика 100 = 1900."""
        result = analyze(test_csv)
        assert "sku_1" in result
        assert "1,900.00" in result

    def test_sku2_profit(self, test_csv):
        """sku_2: доход 2000 (только продажа) - штрафы 500 = 1500."""
        result = analyze(test_csv)
        assert "sku_2" in result
        assert "1,000.00" in result

    def test_sales_income(self, test_csv):
        """Доход = продажи − возвраты: (1000+1000+2000) − 2000 = 2000."""
        result = analyze(test_csv)
        assert "Доход (продажи − возвраты): 2,000.00" in result

    def test_storage_expense(self, test_csv):
        """Хранение = 300."""
        result = analyze(test_csv)
        assert "300.00" in result

    def test_contains_sections(self, test_csv):
        """Отчёт содержит все три секции."""
        result = analyze(test_csv)
        assert "ОБЩИЙ ФИНАНСОВЫЙ ИТОГ" in result
        assert "ПРИБЫЛЬ ПО АРТИКУЛАМ" in result
        assert "ТОП-3" in result

    def test_contains_emoji(self, test_csv):
        """Отчёт содержит эмодзи для визуальных якорей."""
        result = analyze(test_csv)
        assert "💰" in result
        assert "🚚" in result
        assert "🔴" in result

    def test_contains_html_bold(self, test_csv):
        """Отрицательные значения и штрафы выделены жирным."""
        result = analyze(test_csv)
        assert "<b>" in result

    def test_contains_insight(self, test_csv):
        """Блок ТОП-3 содержит инсайт-рекомендацию."""
        result = analyze(test_csv)
        assert "Рекомендация" in result

    def test_invalid_extension(self, tmp_path):
        """Выбрасывает ValueError для неподдерживаемого формата."""
        bad_file = tmp_path / "report.txt"
        bad_file.write_text("hello")
        with pytest.raises(ValueError, match="Неподдерживаемый формат"):
            analyze(str(bad_file))

    def test_fmt_function(self):
        """Форматирование чисел."""
        assert fmt(1234567.89) == "1,234,567.89"
        assert fmt(0) == "0.00"
        assert fmt(-500.5) == "-500.50"

    def test_missing_columns_graceful(self, tmp_path):
        """Скрипт не падает при отсутствии некоторых столбцов."""
        filepath = tmp_path / "minimal.csv"
        df = pd.DataFrame({
            "Артикул поставщика": ["sku_x"],
            "Обоснование для оплаты": ["Продажа"],
            "К перечислению Продавцу за реализованный товар": [5000],
        })
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        result = analyze(str(filepath))
        assert "5,000.00" in result

    def test_empty_report(self, tmp_path):
        """Пустой файл (только заголовки) не вызывает ошибку."""
        filepath = tmp_path / "empty.csv"
        df = pd.DataFrame(columns=[
            "Артикул поставщика",
            "Обоснование для оплаты",
            "К перечислению Продавцу за реализованный товар",
        ])
        df.to_csv(filepath, index=False, encoding="utf-8-sig")
        result = analyze(str(filepath))
        assert "ОБЩИЙ ФИНАНСОВЫЙ ИТОГ" in result
        assert "0.00" in result


# ═══════════════════════════════════════════════════════════════════
# Тесты database.py
# ═══════════════════════════════════════════════════════════════════

class TestDatabase:
    """Тесты модуля базы данных."""

    def test_add_new_user(self, test_db):
        """Новый пользователь добавляется, возвращается True."""
        result = add_user(12345, "testuser", db_path=test_db)
        assert result is True

    def test_add_existing_user(self, test_db):
        """Повторное добавление возвращает False."""
        add_user(12345, "testuser", db_path=test_db)
        result = add_user(12345, "testuser", db_path=test_db)
        assert result is False

    def test_get_user(self, test_db):
        """Данные пользователя корректно извлекаются."""
        add_user(99999, "alice", db_path=test_db)
        user = get_user(99999, db_path=test_db)
        assert user is not None
        assert user["user_id"] == 99999
        assert user["username"] == "alice"
        assert user["report_count"] == 0

    def test_get_nonexistent_user(self, test_db):
        """Несуществующий пользователь возвращает None."""
        user = get_user(77777, db_path=test_db)
        assert user is None

    def test_trial_active_for_new_user(self, test_db):
        """Триал активен сразу после регистрации."""
        add_user(11111, "bob", db_path=test_db)
        assert is_trial_active(11111, db_path=test_db) is True

    def test_trial_expired(self, test_db):
        """Триал истекает через 7 дней."""
        conn = get_connection(test_db)
        old_date = (datetime.utcnow() - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (22222, "expired_user", old_date),
        )
        conn.commit()
        conn.close()

        assert is_trial_active(22222, db_path=test_db) is False

    def test_trial_active_on_day_6(self, test_db):
        """Триал ещё активен на 6-й день."""
        conn = get_connection(test_db)
        recent_date = (datetime.utcnow() - timedelta(days=6)).isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (33333, "active_user", recent_date),
        )
        conn.commit()
        conn.close()

        assert is_trial_active(33333, db_path=test_db) is True

    def test_trial_inactive_for_unknown_user(self, test_db):
        """Триал неактивен для незарегистрированного пользователя."""
        assert is_trial_active(44444, db_path=test_db) is False

    def test_table_created_automatically(self, test_db):
        """Таблицы users и feedback создаются автоматически."""
        conn = get_connection(test_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        assert cursor.fetchone() is not None
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='feedback'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    # ─── Тесты grant_access ─────────────────────────────────────

    def test_grant_access_existing_user(self, test_db):
        """grant_access выдаёт доступ существующему пользователю."""
        add_user(50001, "paid_user", db_path=test_db)
        result = grant_access(50001, 30, db_path=test_db)
        assert result is True
        user = get_user(50001, db_path=test_db)
        assert user["access_until"] is not None

    def test_grant_access_nonexistent_user(self, test_db):
        """grant_access возвращает False для несуществующего пользователя."""
        result = grant_access(99991, 30, db_path=test_db)
        assert result is False

    def test_grant_access_overrides_trial(self, test_db):
        """Оплаченный доступ работает даже если триал истёк."""
        conn = get_connection(test_db)
        old_date = (datetime.utcnow() - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (50002, "old_user", old_date),
        )
        conn.commit()
        conn.close()

        # Триал истёк
        assert is_trial_active(50002, db_path=test_db) is False

        # Выдаём доступ
        grant_access(50002, 90, db_path=test_db)

        # Теперь доступ есть
        assert is_trial_active(50002, db_path=test_db) is True

    # ─── Тесты increment_report_count ────────────────────────────

    def test_increment_report_count(self, test_db):
        """Счётчик отчётов инкрементируется."""
        add_user(60001, "counter_user", db_path=test_db)
        assert increment_report_count(60001, db_path=test_db) == 1
        assert increment_report_count(60001, db_path=test_db) == 2
        assert increment_report_count(60001, db_path=test_db) == 3

    # ─── Тесты feedback ──────────────────────────────────────────

    def test_save_and_check_feedback(self, test_db):
        """Отзыв сохраняется и has_given_feedback возвращает True."""
        add_user(70001, "feedback_user", db_path=test_db)
        assert has_given_feedback(70001, db_path=test_db) is False

        save_feedback(70001, 5, "Отличный бот!", db_path=test_db)
        assert has_given_feedback(70001, db_path=test_db) is True

    def test_no_feedback_by_default(self, test_db):
        """По умолчанию отзыва нет."""
        add_user(70002, "no_feedback", db_path=test_db)
        assert has_given_feedback(70002, db_path=test_db) is False


# ═══════════════════════════════════════════════════════════════════
# Тесты bot.py (юнит-тесты хендлеров)
# ═══════════════════════════════════════════════════════════════════

class TestBotHandlers:
    """Тесты хендлеров бота (без реального Telegram API)."""

    @pytest.fixture(autouse=True)
    def _patch_bot_token(self, monkeypatch):
        """Подставляем фейковый токен, чтобы бот не падал при импорте."""
        monkeypatch.setenv("BOT_TOKEN", "123456:FAKE-TOKEN-FOR-TESTS")
        monkeypatch.setenv("ADMIN_ID", "999000")

    def test_split_text_short(self):
        """Короткий текст не разбивается."""
        from bot import split_text
        chunks = split_text("Hello world", max_len=100)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_split_text_long(self):
        """Длинный текст разбивается на части."""
        from bot import split_text
        long_text = "\n".join([f"Line {i}" for i in range(500)])
        chunks = split_text(long_text, max_len=200)
        assert len(chunks) > 1
        reassembled = "\n".join(chunks)
        for i in range(500):
            assert f"Line {i}" in reassembled

    @pytest.mark.asyncio
    async def test_start_handler_sends_welcome(self, test_db, monkeypatch):
        """Хендлер /start отправляет приветственное сообщение."""
        import database
        monkeypatch.setattr(database, "DB_PATH", test_db)

        from bot import cmd_start, WELCOME_TEXT
        import bot as bot_module
        monkeypatch.setattr(bot_module, "add_user",
                            lambda uid, uname: add_user(uid, uname, db_path=test_db))

        message = AsyncMock()
        message.from_user = MagicMock()
        message.from_user.id = 55555
        message.from_user.username = "test_bot_user"
        message.answer = AsyncMock()

        await cmd_start(message)

        call_args = message.answer.call_args[0]
        assert WELCOME_TEXT in call_args

    @pytest.mark.asyncio
    async def test_handle_other_message(self, monkeypatch):
        """Любое текстовое сообщение от неожидающего пользователя получает подсказку."""
        from bot import handle_other, _pending_feedback

        # Убедимся, что у этого пользователя нет pending feedback
        user_id = 88888
        _pending_feedback.pop(user_id, None)

        message = AsyncMock()
        message.from_user = MagicMock()
        message.from_user.id = user_id
        message.from_user.username = "test_user"
        message.text = "что-то"
        message.answer = AsyncMock()

        await handle_other(message)

        message.answer.assert_called_once()
        call_text = message.answer.call_args[0][0]
        assert ".xlsx" in call_text or "Excel" in call_text

    @pytest.mark.asyncio
    async def test_buy_handler_shows_keyboard(self, monkeypatch):
        """Команда /buy показывает клавиатуру оплаты."""
        from bot import cmd_buy

        message = AsyncMock()
        message.answer = AsyncMock()

        await cmd_buy(message)

        message.answer.assert_called_once()
        call_kwargs = message.answer.call_args
        assert call_kwargs.kwargs.get("parse_mode") == "HTML"
        # Должна быть InlineKeyboardMarkup
        assert call_kwargs.kwargs.get("reply_markup") is not None

    @pytest.mark.asyncio
    async def test_give_access_denied_for_non_admin(self, monkeypatch):
        """give_access отказывает не-админу."""
        from bot import cmd_give_access
        import bot as bot_module
        monkeypatch.setattr(bot_module, "ADMIN_ID", 999000)

        message = AsyncMock()
        message.from_user = MagicMock()
        message.from_user.id = 12345  # не админ
        message.text = "/give_access 55555 30"
        message.answer = AsyncMock()

        await cmd_give_access(message)

        call_text = message.answer.call_args[0][0]
        assert "нет доступа" in call_text


# ═══════════════════════════════════════════════════════════════════
# Интеграционный тест: парсер + БД
# ═══════════════════════════════════════════════════════════════════

class TestIntegration:
    """Интеграционные тесты: полный цикл работы."""

    def test_full_flow_active_trial(self, test_csv, test_db):
        """Полный цикл: регистрация -> проверка триала -> анализ файла."""
        assert add_user(10001, "seller", db_path=test_db) is True
        assert is_trial_active(10001, db_path=test_db) is True

        result = analyze(test_csv)
        assert "К ВЫПЛАТЕ" in result
        assert "400.00" in result

    def test_full_flow_expired_trial(self, test_csv, test_db):
        """Полный цикл: пользователь с истёкшим триалом не может получить отчёт."""
        conn = get_connection(test_db)
        old_date = (datetime.utcnow() - timedelta(days=8)).isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (10002, "old_seller", old_date),
        )
        conn.commit()
        conn.close()

        assert is_trial_active(10002, db_path=test_db) is False

    def test_full_flow_paid_access(self, test_csv, test_db):
        """Полный цикл: оплаченный доступ даёт возможность анализа."""
        # Старый триал
        conn = get_connection(test_db)
        old_date = (datetime.utcnow() - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date) VALUES (?, ?, ?)",
            (10003, "paid_seller", old_date),
        )
        conn.commit()
        conn.close()

        assert is_trial_active(10003, db_path=test_db) is False

        # Оплата
        grant_access(10003, 365, db_path=test_db)
        assert is_trial_active(10003, db_path=test_db) is True

        # Анализ
        result = analyze(test_csv)
        assert "К ВЫПЛАТЕ" in result
