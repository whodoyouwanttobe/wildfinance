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
        "ALTER TABLE users ADD COLUMN source TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN last_seen TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN visits INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN actions INTEGER DEFAULT 0",
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS costs (
            user_id     INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            article     TEXT NOT NULL,
            cost        REAL NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (user_id, marketplace, article)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id  INTEGER PRIMARY KEY,
            tax_mode TEXT,
            tax_rate REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS last_reports (
            user_id    INTEGER PRIMARY KEY,
            marketplace TEXT NOT NULL,
            file_name  TEXT,
            snapshot   TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            label      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            plan       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            paid       INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sales_daily (
            user_id     INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            day         TEXT NOT NULL,
            article     TEXT NOT NULL,
            label       TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            qty         REAL DEFAULT 0,
            payout      REAL DEFAULT 0,
            revenue     REAL DEFAULT 0,
            PRIMARY KEY (user_id, marketplace, day, article)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_ranges (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            marketplace TEXT NOT NULL,
            start_day   TEXT NOT NULL,
            end_day     TEXT NOT NULL,
            dated       INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_events (
            user_id    INTEGER NOT NULL,
            event      TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_events_user ON user_events (user_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            charge_id   TEXT UNIQUE NOT NULL,
            user_id     INTEGER NOT NULL,
            plan        TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            url         TEXT DEFAULT '',
            created_at  TEXT NOT NULL,
            issued_at   TEXT DEFAULT NULL
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


# ─── Ожидающие оплаты ЮMoney ────────────────────────────────────────────────

def add_pending_payment(label: str, user_id: int, plan: str, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pending_payments (label, user_id, plan, created_at, paid) "
            "VALUES (?, ?, ?, ?, 0)",
            (label, user_id, plan, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_payments(
    user_id: int | None = None, max_age_hours: int = 48, db_path: str = DB_PATH,
) -> list[dict]:
    """Неоплаченные счета не старше max_age_hours (опционально — одного пользователя)."""
    conn = get_connection(db_path)
    try:
        since = (datetime.utcnow() - timedelta(hours=max_age_hours)).isoformat()
        sql = "SELECT label, user_id, plan, created_at FROM pending_payments WHERE paid = 0 AND created_at >= ?"
        args: list = [since]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        rows = conn.execute(sql, args).fetchall()
        return [{"label": r[0], "user_id": r[1], "plan": r[2], "created_at": r[3]} for r in rows]
    finally:
        conn.close()


def mark_pending_paid(label: str, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE pending_payments SET paid = 1 WHERE label = ?", (label,))
        conn.commit()
    finally:
        conn.close()


# ─── Источники трафика (t.me/bot?start=<источник>) ─────────────────────────

def set_user_source(user_id: int, source: str, db_path: str = DB_PATH) -> None:
    """Запоминает, откуда пришёл пользователь (только если источник ещё не задан)."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE users SET source = ? WHERE user_id = ? AND (source IS NULL OR source = '')",
            (source, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_source_stats(db_path: str = DB_PATH) -> list[dict]:
    """По каждому источнику: сколько пришло, сколько прислали отчёт, сколько оплатили и на какую сумму."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("""
            SELECT COALESCE(NULLIF(u.source, ''), '(без метки)') AS src,
                   COUNT(*)                                        AS users,
                   SUM(CASE WHEN u.report_count > 0 THEN 1 ELSE 0 END) AS active,
                   COUNT(DISTINCT p.user_id)                       AS payers,
                   COALESCE(SUM(p.amount), 0)                      AS revenue
            FROM users u
            LEFT JOIN payments p ON p.user_id = u.user_id
            GROUP BY src
            ORDER BY users DESC
        """).fetchall()
        return [
            {"source": r[0], "users": r[1], "active": r[2], "payers": r[3], "revenue_kop": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


# ─── Себестоимость, налоги, последний отчёт ─────────────────────────────────

def set_costs(user_id: int, marketplace: str, costs: dict[str, float], db_path: str = DB_PATH) -> int:
    """Сохраняет себестоимость (за 1 шт) по артикулам. Возвращает число сохранённых."""
    if not costs:
        return 0
    conn = get_connection(db_path)
    try:
        now = datetime.utcnow().isoformat()
        conn.executemany(
            "INSERT INTO costs (user_id, marketplace, article, cost, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, marketplace, article) DO UPDATE SET cost = excluded.cost, updated_at = excluded.updated_at",
            [(user_id, marketplace, a, float(c), now) for a, c in costs.items()],
        )
        conn.commit()
        return len(costs)
    finally:
        conn.close()


def get_costs(user_id: int, marketplace: str | None = None, db_path: str = DB_PATH) -> dict:
    """{артикул: цена} для маркетплейса или {маркетплейс: {артикул: цена}} для всех."""
    conn = get_connection(db_path)
    try:
        if marketplace:
            rows = conn.execute(
                "SELECT article, cost FROM costs WHERE user_id = ? AND marketplace = ? ORDER BY article",
                (user_id, marketplace),
            ).fetchall()
            return {a: c for a, c in rows}
        rows = conn.execute(
            "SELECT marketplace, article, cost FROM costs WHERE user_id = ? ORDER BY marketplace, article",
            (user_id,),
        ).fetchall()
        out: dict = {}
        for mp, a, c in rows:
            out.setdefault(mp, {})[a] = c
        return out
    finally:
        conn.close()


def delete_cost(user_id: int, marketplace: str, article: str, db_path: str = DB_PATH) -> bool:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM costs WHERE user_id = ? AND marketplace = ? AND article = ?",
            (user_id, marketplace, article),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_tax(user_id: int, db_path: str = DB_PATH) -> tuple[str | None, float]:
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT tax_mode, tax_rate FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return (row[0], float(row[1] or 0)) if row else (None, 0.0)
    finally:
        conn.close()


def set_tax(user_id: int, mode: str, rate: float, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO user_settings (user_id, tax_mode, tax_rate) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tax_mode = excluded.tax_mode, tax_rate = excluded.tax_rate",
            (user_id, mode, rate),
        )
        conn.commit()
    finally:
        conn.close()


def save_last_report(user_id: int, snapshot: dict, file_name: str = "", db_path: str = DB_PATH) -> None:
    """Снимок последнего отчёта — чтобы ввод себестоимости работал и после перезапуска бота."""
    import json
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO last_reports (user_id, marketplace, file_name, snapshot, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET marketplace = excluded.marketplace, file_name = excluded.file_name, "
            "snapshot = excluded.snapshot, created_at = excluded.created_at",
            (user_id, snapshot.get("marketplace", ""), file_name,
             json.dumps(snapshot, ensure_ascii=False), datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_report(user_id: int, db_path: str = DB_PATH) -> dict | None:
    import json
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT snapshot, file_name FROM last_reports WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        snap = json.loads(row[0])
        snap["file_name"] = row[1] or ""
        return snap
    finally:
        conn.close()


# ─── Чеки «Мой налог» ─────────────────────────────────────────────────────────

def add_receipt(charge_id: str, user_id: int, plan: str, amount: int, db_path: str = DB_PATH) -> int:
    """Создаёт запись «нужен чек» для платежа. Возвращает номер чека (id)."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO receipts (charge_id, user_id, plan, amount, created_at) VALUES (?, ?, ?, ?, ?)",
            (charge_id, user_id, plan, amount, datetime.utcnow().isoformat()),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM receipts WHERE charge_id = ?", (charge_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _receipt_row(row) -> dict | None:
    if not row:
        return None
    keys = ("id", "charge_id", "user_id", "plan", "amount", "url", "created_at", "issued_at")
    return dict(zip(keys, row))


_RECEIPT_COLS = "id, charge_id, user_id, plan, amount, url, created_at, issued_at"


def set_receipt_url(receipt_id: int, url: str, db_path: str = DB_PATH) -> dict | None:
    """Сохраняет ссылку на чек. Возвращает запись или None, если номера нет."""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE receipts SET url = ?, issued_at = ? WHERE id = ?",
            (url, datetime.utcnow().isoformat(), receipt_id),
        )
        conn.commit()
        return _receipt_row(conn.execute(
            f"SELECT {_RECEIPT_COLS} FROM receipts WHERE id = ?", (receipt_id,)).fetchone())
    finally:
        conn.close()


def get_user_receipts(user_id: int, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_RECEIPT_COLS} FROM receipts WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)
        ).fetchall()
        return [_receipt_row(r) for r in rows]
    finally:
        conn.close()


def get_pending_receipts(db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_RECEIPT_COLS} FROM receipts WHERE url = '' OR url IS NULL ORDER BY id"
        ).fetchall()
        return [_receipt_row(r) for r in rows]
    finally:
        conn.close()


# ─── Удаление данных по запросу пользователя (152-ФЗ) ────────────────────────

def delete_user_data(user_id: int, db_path: str = DB_PATH) -> None:
    """
    Удаляет себестоимость, налоговые настройки, показатели и историю отчётов, отзывы,
    неоплаченные счета и имя пользователя. Платежи и чеки остаются
    (их нужно хранить по налоговому законодательству), строка users —
    чтобы не выдавать пробный период повторно.
    """
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM costs WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM last_reports WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM feedback WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM pending_payments WHERE user_id = ? AND paid = 0", (user_id,))
        conn.execute("DELETE FROM sales_daily WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM report_ranges WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_events WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE users SET username = NULL, source = '' WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# ─── События воронки (для CRM: перешёл, смотрел пример, прислал отчёт, оплатил) ─

def log_event(user_id: int, event: str, db_path: str | None = None) -> None:
    conn = get_connection(db_path or DB_PATH)
    try:
        conn.execute(
            "INSERT INTO user_events (user_id, event, created_at) VALUES (?, ?, ?)",
            (user_id, event, datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def get_crm_rows(db_path: str | None = None) -> list[dict]:
    """Все пользователи с воронкой: события, отчёты, оплаты. Только для админки/CRM."""
    conn = get_connection(db_path or DB_PATH)
    try:
        users = conn.execute(
            "SELECT user_id, username, source, join_date, report_count, access_until, "
            "last_seen, visits, actions FROM users"
        ).fetchall()
        events: dict[int, dict] = {}
        for uid, ev, cnt, first, last in conn.execute(
            "SELECT user_id, event, COUNT(*), MIN(created_at), MAX(created_at) "
            "FROM user_events GROUP BY user_id, event"
        ):
            events.setdefault(uid, {})[ev] = {"count": cnt, "first": first, "last": last}
        last_rep = {
            uid: (mp, created)
            for uid, mp, created in conn.execute("SELECT user_id, marketplace, created_at FROM last_reports")
        }
        paid = {
            uid: (cnt, total)
            for uid, cnt, total in conn.execute(
                "SELECT user_id, COUNT(*), SUM(amount) FROM payments GROUP BY user_id"
            )
        }
        rows = []
        for uid, username, source, join_date, reports, access_until, last_seen, visits, actions in users:
            cnt, total = paid.get(uid, (0, 0))
            rows.append({
                "user_id": uid,
                "username": username or "",
                "source": source or "",
                "join_date": join_date,
                "reports": reports or 0,
                "access_until": access_until,
                "payments": cnt,
                "paid_rub": round((total or 0) / 100),
                "events": events.get(uid, {}),
                "last_report_mp": last_rep.get(uid, (None, None))[0],
                "last_report_at": last_rep.get(uid, (None, None))[1],
                "last_seen": last_seen,
                "visits": visits or 0,
                "actions": actions or 0,
            })
        return rows
    finally:
        conn.close()


# ─── Активность: когда заходил последний раз и как часто ─────────────────────

VISIT_GAP_MINUTES = 30   # перерыв дольше этого — новый «заход»


def touch_user(user_id: int, username: str | None = None, db_path: str | None = None,
               now: datetime | None = None) -> None:
    """Отмечает действие пользователя: last_seen, число действий и заходов (сессий)."""
    now = now or datetime.utcnow()
    conn = get_connection(db_path or DB_PATH)
    try:
        row = conn.execute("SELECT last_seen FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            return  # пользователя ещё нет — его создаст /start
        new_visit = True
        if row[0]:
            try:
                new_visit = now - datetime.fromisoformat(row[0]) > timedelta(minutes=VISIT_GAP_MINUTES)
            except ValueError:
                pass
        conn.execute(
            "UPDATE users SET last_seen = ?, actions = COALESCE(actions, 0) + 1, "
            "visits = COALESCE(visits, 0) + ?, username = COALESCE(?, username) WHERE user_id = ?",
            (now.isoformat(timespec="seconds"), int(new_visit), username or None, user_id),
        )
        conn.commit()
    finally:
        conn.close()
