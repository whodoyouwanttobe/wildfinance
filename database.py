"""
database.py — Работа с SQLite базой данных users.db.

Таблицы:
  users:
    - user_id      INTEGER PRIMARY KEY  (Telegram user ID)
    - username     TEXT                  (Telegram username)
    - join_date    TEXT                  (ISO-формат даты регистрации)
    - access_until TEXT                  (NULL = триал, иначе — дата окончания доступа)
    - report_count INTEGER DEFAULT 0    (кол-во загруженных отчётов)

  payments:
    - charge_id    TEXT PRIMARY KEY     (telegram_payment_charge_id — защита от двойного зачисления)
    - user_id, plan, amount (коп.), provider_charge_id, created_at

  feedback:
    - id           INTEGER PRIMARY KEY AUTOINCREMENT
    - user_id      INTEGER              (Telegram user ID)
    - stars        INTEGER              (1-5)
    - text         TEXT                  (отзыв пользователя)
    - created_at   TEXT                  (дата отзыва)
"""

import sqlite3
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# Если задана переменная DATA_DIR (например, /app/data в Railway),
# база будет сохранена там. Иначе — в текущей папке скрипта.
_default_dir = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", _default_dir)
DB_PATH = os.path.join(DATA_DIR, "users.db")

FREE_TRIAL_DAYS = 7


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Создаёт подключение к БД и таблицы, если их нет. Мигрирует старые схемы."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      INTEGER PRIMARY KEY,
            username     TEXT,
            join_date    TEXT NOT NULL,
            access_until TEXT DEFAULT NULL,
            report_count INTEGER DEFAULT 0
        )
    """)
    # Миграция: добавляем новые столбцы, если таблица уже существовала
    for alter in (
        "ALTER TABLE users ADD COLUMN access_until TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN report_count INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(alter)
        except sqlite3.OperationalError:
            pass  # столбец уже существует

    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            stars      INTEGER NOT NULL,
            text       TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            charge_id          TEXT PRIMARY KEY,
            user_id            INTEGER NOT NULL,
            plan               TEXT NOT NULL,
            amount             INTEGER NOT NULL,
            provider_charge_id TEXT DEFAULT '',
            created_at         TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def add_user(user_id: int, username: str | None = None, db_path: str = DB_PATH) -> bool:
    """
    Добавляет пользователя в БД, если его ещё нет.
    Возвращает True если пользователь новый, False если уже существовал.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone() is not None:
            return False

        now = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO users (user_id, username, join_date, report_count) VALUES (?, ?, ?, 0)",
            (user_id, username or "", now),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def is_trial_active(user_id: int, db_path: str = DB_PATH) -> bool:
    """
    Проверяет, есть ли у пользователя доступ.
    Приоритет: access_until (оплаченный) → триал (7 дней).
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT join_date, access_until FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return False

        join_date_str, access_until_str = row

        # Если есть оплаченный доступ — проверяем его
        if access_until_str:
            access_until = datetime.fromisoformat(access_until_str)
            return datetime.utcnow() < access_until

        # Иначе — стандартный 7-дневный триал
        join_date = datetime.fromisoformat(join_date_str)
        return datetime.utcnow() < join_date + timedelta(days=FREE_TRIAL_DAYS)
    finally:
        conn.close()


def get_user(user_id: int, db_path: str = DB_PATH) -> dict | None:
    """Возвращает данные пользователя или None."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT user_id, username, join_date, access_until, report_count "
            "FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0],
            "username": row[1],
            "join_date": row[2],
            "access_until": row[3],
            "report_count": row[4],
        }
    finally:
        conn.close()


def get_all_users_stats(db_path: str = DB_PATH) -> list[dict]:
    """Возвращает список всех пользователей для админки."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT user_id, username, join_date, access_until, report_count "
            "FROM users ORDER BY join_date DESC"
        )
        users = []
        for row in cursor.fetchall():
            users.append({
                "user_id": row[0],
                "username": row[1],
                "join_date": row[2],
                "access_until": row[3],
                "report_count": row[4],
            })
        return users
    finally:
        conn.close()


def grant_access(user_id: int, days: int, db_path: str = DB_PATH) -> bool:
    """
    Выдаёт пользователю доступ на N дней от текущего момента.
    Возвращает True если пользователь найден, False если нет.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if cursor.fetchone() is None:
            return False

        until = (datetime.utcnow() + timedelta(days=days)).isoformat()
        conn.execute(
            "UPDATE users SET access_until = ? WHERE user_id = ?",
            (until, user_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def increment_report_count(user_id: int, db_path: str = DB_PATH) -> int:
    """
    Увеличивает счётчик отчётов пользователя на 1.
    Возвращает новое значение счётчика.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE users SET report_count = report_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        cursor = conn.execute(
            "SELECT report_count FROM users WHERE user_id = ?", (user_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def save_feedback(
    user_id: int, stars: int, text: str = "", db_path: str = DB_PATH
) -> None:
    """Сохраняет отзыв пользователя."""
    conn = get_connection(db_path)
    try:
        now = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO feedback (user_id, stars, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, stars, text, now),
        )
        conn.commit()
    finally:
        conn.close()


def has_given_feedback(user_id: int, db_path: str = DB_PATH) -> bool:
    """Проверяет, оставлял ли пользователь отзыв."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "SELECT 1 FROM feedback WHERE user_id = ?", (user_id,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def extend_access(user_id: int, days: int, db_path: str = DB_PATH) -> str | None:
    """
    Продлевает доступ на N дней. Если оплаченный доступ ещё активен — дни
    прибавляются к его концу (оплата заранее не «сгорает»), иначе — от текущего момента.
    Возвращает новую дату окончания (ISO) или None, если пользователя нет.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT access_until FROM users WHERE user_id = ?", (user_id,),
        ).fetchone()
        if row is None:
            return None
        now = datetime.utcnow()
        start = now
        if row[0]:
            current = datetime.fromisoformat(row[0])
            if current > now:
                start = current
        until = (start + timedelta(days=days)).isoformat()
        conn.execute("UPDATE users SET access_until = ? WHERE user_id = ?", (until, user_id))
        conn.commit()
        return until
    finally:
        conn.close()


def record_payment(
    charge_id: str,
    user_id: int,
    plan: str,
    amount: int,
    provider_charge_id: str = "",
    db_path: str = DB_PATH,
) -> bool:
    """
    Сохраняет платёж. Возвращает False, если платёж с таким charge_id
    уже был обработан (повторная доставка апдейта) — тогда доступ не продлеваем.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO payments (charge_id, user_id, plan, amount, provider_charge_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (charge_id, user_id, plan, amount, provider_charge_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def get_payments_total(db_path: str = DB_PATH) -> tuple[int, int]:
    """(количество платежей, сумма в копейках) — для админки."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM payments").fetchone()
        return int(row[0]), int(row[1])
    finally:
        conn.close()
