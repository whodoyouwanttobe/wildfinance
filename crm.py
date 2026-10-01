"""
crm.py — мини-API для CRM в Obsidian: кто перешёл по ссылке, смотрел пример,
прислал отчёт, оплатил.

Включается переменными окружения:
    CRM_TOKEN — длинный секрет (без него сервер не запускается)
    CRM_PORT  — порт (по умолчанию 8088)

GET /crm  с заголовком  Authorization: Bearer <CRM_TOKEN>  (или ?token=...)
→ JSON {"generated_at": ..., "bot": "wildfinance_bot", "users": [...]}

Сопоставление с заметкой в Obsidian — по username или по метке ссылки
?start=u_<username> (users.source).
"""

from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime

import database

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return len(os.getenv("CRM_TOKEN", "")) >= 16


def authorized(header: str | None, query_token: str | None) -> bool:
    token = os.getenv("CRM_TOKEN", "")
    if len(token) < 16:
        return False
    given = ""
    if header and header.startswith("Bearer "):
        given = header[7:].strip()
    elif query_token:
        given = query_token.strip()
    return bool(given) and hmac.compare_digest(given, token)


def build_payload(db_path: str | None = None) -> dict:
    rows = database.get_crm_rows(db_path or database.DB_PATH)
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "bot": os.getenv("BOT_USERNAME", "@wildfinance_bot").lstrip("@"),
        "users": rows,
    }


CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
}


async def start_server() -> None:
    """Запускает HTTP-сервер в том же процессе, что и бот (aiohttp идёт вместе с aiogram)."""
    if not enabled():
        logger.info("CRM API выключен (CRM_TOKEN не задан или короче 16 символов)")
        return
    from aiohttp import web

    async def handle(request: "web.Request") -> "web.Response":
        if request.method == "OPTIONS":
            return web.Response(headers=CORS)
        if not authorized(request.headers.get("Authorization"), request.query.get("token")):
            return web.json_response({"error": "unauthorized"}, status=401, headers=CORS)
        return web.json_response(build_payload(), headers=CORS)

    app = web.Application()
    app.router.add_route("GET", "/crm", handle)
    app.router.add_route("OPTIONS", "/crm", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("CRM_PORT", "8088"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("CRM API слушает порт %s", port)
