"""
legal.py — публичная оферта и политика обработки персональных данных.

Тексты лежат в legal/*.md с подстановками {{seller_name}}, {{seller_inn}} и т.д.
Реквизиты берутся из .env:
    SELLER_NAME   — ФИО самозанятого, например «Иванов Иван Иванович»
    SELLER_INN    — ИНН (12 цифр)
    SELLER_EMAIL  — e-mail для претензий и запросов по персональным данным
    BOT_USERNAME  — @имя бота (по умолчанию @wildfinance_bot)
    LEGAL_DATE    — дата редакции документов (по умолчанию 01.10.2026)

Публикация: при старте бот сам выкладывает документы на telegra.ph
(без регистрации) и запоминает ссылки в DATA_DIR/legal_pages.json.
Если текст или реквизиты изменились — страница обновляется по той же ссылке.
Можно задать свои ссылки вручную: OFFER_URL / PRIVACY_URL — тогда
публикация не выполняется.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGAL_DIR = os.path.join(BASE_DIR, "legal")
DOCS = {
    "offer": "offer.md",
    "privacy": "privacy.md",
}
TELEGRAPH_API = "https://api.telegra.ph"


def _data_dir() -> str:
    return os.getenv("DATA_DIR", BASE_DIR)


def _state_path() -> str:
    return os.path.join(_data_dir(), "legal_pages.json")


def seller() -> dict:
    return {
        "seller_name": os.getenv("SELLER_NAME", "").strip(),
        "seller_inn": os.getenv("SELLER_INN", "").strip(),
        "seller_email": os.getenv("SELLER_EMAIL", "").strip(),
        "bot_username": os.getenv("BOT_USERNAME", "@wildfinance_bot").strip() or "@wildfinance_bot",
        "doc_date": os.getenv("LEGAL_DATE", "01.10.2026").strip() or "01.10.2026",
    }


def configured() -> bool:
    """Реквизиты заполнены — документы можно публиковать."""
    s = seller()
    return bool(s["seller_name"] and re.fullmatch(r"\d{10}|\d{12}", s["seller_inn"]) and s["seller_email"])


def render(kind: str) -> str:
    """Markdown-текст документа с подставленными реквизитами."""
    with open(os.path.join(LEGAL_DIR, DOCS[kind]), encoding="utf-8") as f:
        text = f.read()
    values = seller()
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value or f"[{key}]")
    return text


def title(kind: str) -> str:
    for line in render(kind).splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return kind


# ─── Markdown → Telegraph / Telegram ─────────────────────────────────────────

_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline_nodes(text: str) -> list:
    """'a **b** c' → ['a ', {'tag': 'strong', 'children': ['b']}, ' c']"""
    nodes, pos = [], 0
    for m in _BOLD.finditer(text):
        if m.start() > pos:
            nodes.append(text[pos:m.start()])
        nodes.append({"tag": "strong", "children": [m.group(1)]})
        pos = m.end()
    if pos < len(text):
        nodes.append(text[pos:])
    return nodes or [""]


def _blocks(md: str) -> list[tuple[str, list[str]]]:
    """Разбивает markdown на блоки: ('h1'|'h2'|'p'|'ul', строки)."""
    blocks: list[tuple[str, list[str]]] = []
    para: list[str] = []
    items: list[str] = []

    def flush():
        if para:
            blocks.append(("p", para.copy()))
            para.clear()
        if items:
            blocks.append(("ul", items.copy()))
            items.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
        elif line.startswith("# "):
            flush()
            blocks.append(("h1", [line[2:].strip()]))
        elif line.startswith("## "):
            flush()
            blocks.append(("h2", [line[3:].strip()]))
        elif line.startswith("- "):
            if para:
                flush()
            items.append(line[2:].strip())
        else:
            if items:
                flush()
            para.append(line.strip())
    flush()
    return blocks


def to_telegraph_nodes(md: str) -> list:
    nodes: list = []
    for kind, lines in _blocks(md):
        if kind == "h1":
            continue  # заголовок страницы передаётся отдельно
        if kind == "h2":
            nodes.append({"tag": "h3", "children": lines})
        elif kind == "ul":
            nodes.append({"tag": "ul", "children": [
                {"tag": "li", "children": _inline_nodes(item)} for item in lines
            ]})
        else:
            children: list = []
            for i, line in enumerate(lines):
                if i:
                    children.append({"tag": "br"})
                children.extend(_inline_nodes(line))
            nodes.append({"tag": "p", "children": children})
    return nodes


def _inline_html(text: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))


def to_telegram_chunks(md: str, limit: int = 3800) -> list[str]:
    """Документ в виде HTML-сообщений Telegram (каждое ≤ limit символов)."""
    parts: list[str] = []
    for kind, lines in _blocks(md):
        if kind == "h1":
            parts.append(f"📄 <b>{html.escape(lines[0])}</b>")
        elif kind == "h2":
            parts.append(f"<b>{html.escape(lines[0])}</b>")
        elif kind == "ul":
            parts.append("\n".join(f"• {_inline_html(x)}" for x in lines))
        else:
            parts.append("\n".join(_inline_html(x) for x in lines))
    chunks, cur = [], ""
    for part in parts:
        candidate = f"{cur}\n\n{part}" if cur else part
        if len(candidate) > limit and cur:
            chunks.append(cur)
            cur = part
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


# ─── Ссылки и публикация ────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    tmp = _state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _state_path())


def url(kind: str) -> str:
    """Ссылка на документ: из .env (OFFER_URL / PRIVACY_URL) или опубликованная ботом."""
    manual = os.getenv(f"{kind.upper()}_URL", "").strip()
    if manual:
        return manual
    return _load_state().get("pages", {}).get(kind, {}).get("url", "")


def links_html() -> str:
    """«оферты и политики…» со ссылками (или с командами, если ссылок нет)."""
    offer, privacy = url("offer"), url("privacy")
    o = f'<a href="{html.escape(offer)}">публичной оферты</a>' if offer else "публичной оферты (/terms)"
    p = (f'<a href="{html.escape(privacy)}">политикой обработки персональных данных</a>'
         if privacy else "политикой обработки персональных данных (/privacy)")
    return f"Оплачивая, вы принимаете условия {o} и соглашаетесь с {p}."


def _digest(kind: str) -> str:
    return hashlib.sha256(render(kind).encode("utf-8")).hexdigest()[:16]


async def _telegraph(client, method: str, payload: dict) -> dict:
    resp = await client.post(f"{TELEGRAPH_API}/{method}", data=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"telegra.ph {method}: {data.get('error')}")
    return data["result"]


async def ensure_published() -> dict:
    """
    Публикует/обновляет документы на telegra.ph. Возвращает {kind: url}.
    Ничего не делает, если реквизиты не заполнены или ссылки заданы вручную.
    """
    if not configured():
        logger.warning("Оферта: не заданы SELLER_NAME / SELLER_INN / SELLER_EMAIL — документы не опубликованы")
        return {}
    import httpx

    state = _load_state()
    pages = state.setdefault("pages", {})
    result = {}
    async with httpx.AsyncClient() as client:
        if not state.get("token"):
            acc = await _telegraph(client, "createAccount", {
                "short_name": "WildFinance",
                "author_name": "WildFinance",
            })
            state["token"] = acc["access_token"]
            _save_state(state)
        for kind in DOCS:
            if os.getenv(f"{kind.upper()}_URL", "").strip():
                continue
            digest = _digest(kind)
            page = pages.get(kind, {})
            if page.get("hash") == digest and page.get("url"):
                result[kind] = page["url"]
                continue
            payload = {
                "access_token": state["token"],
                "title": title(kind),
                "author_name": "WildFinance",
                "content": json.dumps(to_telegraph_nodes(render(kind)), ensure_ascii=False),
            }
            if page.get("path"):
                res = await _telegraph(client, f"editPage/{page['path']}", payload)
            else:
                res = await _telegraph(client, "createPage", payload)
            pages[kind] = {"path": res["path"], "url": res["url"], "hash": digest}
            _save_state(state)
            result[kind] = res["url"]
            logger.info("Документ «%s» опубликован: %s", kind, res["url"])
    return result
