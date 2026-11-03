"""
reviews.py — отзывы с сайта sdt-solutions.ru с модерацией в Telegram.

Сайт отправляет форму на  POST /review  (через Caddy: https://sdt-solutions.ru/api/review),
бот присылает админу отзыв с кнопками «Опубликовать / Отклонить».
Опубликованные отзывы сайт читает с  GET /reviews.

Работает на том же HTTP-сервере, что и CRM (crm.py, порт CRM_PORT).
REVIEW_PROXY_IP — IP сервера сайта: только ему верим в заголовке X-Forwarded-For (для лимита).
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Awaitable, Callable

import database

logger = logging.getLogger(__name__)

# bot.py подставляет сюда отправку админу: async def(review: dict) -> None
on_new: Callable[[dict], Awaitable[None]] | None = None

# Как подписать отзыв на сайте. Админу в Telegram всегда приходит всё, что человек указал.
SIGN = {
    "full": "имя, должность и компания",
    "no_company": "без названия компании",
    "anon": "анонимно (только должность)",
}

LIMITS = {"name": 80, "role": 120, "company": 120, "solution": 60, "text": 1500}
MIN_TEXT = 20
PER_HOUR = 3
_hits: dict[str, deque] = defaultdict(deque)

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


def _conn(db_path: str | None = None):
    conn = database.get_connection(db_path or database.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS site_reviews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            role       TEXT DEFAULT '',
            company    TEXT DEFAULT '',
            solution   TEXT DEFAULT '',
            text       TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',
            ip         TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            decided_at TEXT DEFAULT NULL
        )
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(site_reviews)")}
    if "sign" not in cols:
        conn.execute("ALTER TABLE site_reviews ADD COLUMN sign TEXT NOT NULL DEFAULT 'full'")
    if "stars" not in cols:
        conn.execute("ALTER TABLE site_reviews ADD COLUMN stars INTEGER NOT NULL DEFAULT 0")
    return conn


def validate(body: dict) -> dict:
    """Чистит поля формы. Бросает ValueError с понятным текстом для посетителя."""
    if not isinstance(body, dict):
        raise ValueError("Неверный формат")
    if str(body.get("website", "")).strip():          # поле-ловушка для ботов, человек его не видит
        raise ValueError("spam")
    clean = {k: re.sub(r"\s+", " ", str(body.get(k, "") or "")).strip()[:n] for k, n in LIMITS.items()}
    clean["text"] = str(body.get("text", "") or "").strip()[:LIMITS["text"]]
    clean["solution"] = re.sub(r"[^a-z0-9_-]", "", clean["solution"].lower())
    if not clean["name"]:
        raise ValueError("Укажите имя")
    if len(clean["text"]) < MIN_TEXT:
        raise ValueError(f"Напишите хотя бы пару предложений (от {MIN_TEXT} символов)")
    clean["sign"] = body.get("sign") if body.get("sign") in SIGN else "full"
    try:
        clean["stars"] = int(body.get("stars"))
    except (TypeError, ValueError):
        clean["stars"] = 0
    if not 1 <= clean["stars"] <= 5:
        raise ValueError("Поставьте оценку от 1 до 5 звёзд")
    if body.get("consent") is not True:
        raise ValueError("Нужно согласие на публикацию отзыва")
    return clean


def allowed(ip: str, now: float | None = None) -> bool:
    """Не больше PER_HOUR отзывов в час с одного адреса."""
    now = now or time.time()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= PER_HOUR:
        return False
    q.append(now)
    return True


def add(r: dict, ip: str = "", db_path: str | None = None) -> int:
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO site_reviews (name, role, company, solution, text, sign, stars, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (r["name"], r["role"], r["company"], r["solution"], r["text"], r.get("sign", "full"), int(r.get("stars", 0)), ip[:45],
             datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def set_status(review_id: int, status: str, db_path: str | None = None) -> dict | None:
    assert status in ("approved", "rejected", "pending")
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE site_reviews SET status = ?, decided_at = ? WHERE id = ?",
                     (status, datetime.utcnow().isoformat(timespec="seconds"), review_id))
        conn.commit()
        row = conn.execute("SELECT id, name, role, company, solution, text, status FROM site_reviews WHERE id = ?",
                           (review_id,)).fetchone()
    finally:
        conn.close()
    keys = ("id", "name", "role", "company", "solution", "text", "status")
    return dict(zip(keys, row)) if row else None


def public(r: dict) -> dict:
    """Что видно на сайте: скрытое по просьбе клиента наружу не отдаём."""
    out = {k: r[k] for k in ("id", "name", "role", "company", "solution", "text", "date", "stars")}
    if r["sign"] in ("no_company", "anon"):
        out["company"] = ""
    if r["sign"] == "anon":
        out["name"] = "Имя и компания скрыты по просьбе клиента"
    return out


def approved(db_path: str | None = None) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT id, name, role, company, solution, text, created_at, sign, stars FROM site_reviews "
                            "WHERE status = 'approved' ORDER BY created_at DESC LIMIT 100").fetchall()
    finally:
        conn.close()
    keys = ("id", "name", "role", "company", "solution", "text", "date", "sign", "stars")
    return [public(dict(zip(keys, r)) | {"date": r[6][:10]}) for r in rows]


def client_ip(request) -> str:
    remote = request.remote or ""
    if remote == os.getenv("REVIEW_PROXY_IP", "195.2.78.35"):
        return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()) or remote
    return remote


def setup(app) -> None:
    """Добавляет маршруты /review и /reviews к aiohttp-приложению CRM."""
    from aiohttp import web

    async def post_review(request: "web.Request") -> "web.Response":
        if request.method == "OPTIONS":
            return web.Response(headers=CORS)
        try:
            data = validate(await request.json())
        except ValueError as exc:
            if str(exc) == "spam":                    # боту отвечаем «успешно», чтобы не подбирал
                return web.json_response({"ok": True}, headers=CORS)
            return web.json_response({"error": str(exc)}, status=400, headers=CORS)
        except Exception:
            return web.json_response({"error": "Неверный формат"}, status=400, headers=CORS)
        ip = client_ip(request)
        if not allowed(ip):
            return web.json_response({"error": "Слишком много отзывов подряд, попробуйте позже"}, status=429, headers=CORS)
        rid = add(data, ip)
        if on_new:
            try:
                await on_new(data | {"id": rid})
            except Exception:
                logger.exception("Не удалось отправить отзыв на модерацию")
        return web.json_response({"ok": True}, headers=CORS)

    async def get_reviews(request: "web.Request") -> "web.Response":
        return web.json_response({"reviews": approved()}, headers=CORS | {"Cache-Control": "max-age=60"})

    app.router.add_route("POST", "/review", post_review)
    app.router.add_route("OPTIONS", "/review", post_review)
    app.router.add_route("GET", "/reviews", get_reviews)
