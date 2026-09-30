"""
yoomoney_pay.py — Автооплата через кошелёк ЮMoney (без ЮKassa и без вебхук-сервера).

Схема:
  1. Пользователь жмёт тариф → бот создаёт уникальную метку (label) вида
     wf:month:123456789:a1b2c3 и ссылку на форму оплаты ЮMoney (Quickpay) с этой меткой.
  2. Пользователь платит картой или из кошелька ЮMoney.
  3. Бот раз в 30 секунд (и по кнопке «Я оплатил») запрашивает историю входящих
     переводов (API operation-history) и ищет платёж с этой меткой.
  4. Нашёл успешный платёж → выдаёт доступ. operation_id сохраняется в таблицу payments,
     поэтому один платёж не засчитается дважды.

Нужно в .env:
  YOOMONEY_WALLET=4100...        — номер кошелька ЮMoney (куда приходят деньги)
  YOOMONEY_TOKEN=4100....        — OAuth-токен с правом operation-history
                                   (получить: python tools/yoomoney_get_token.py)
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

API_BASE = "https://yoomoney.ru"
QUICKPAY_URL = f"{API_BASE}/quickpay/confirm"
HISTORY_URL = f"{API_BASE}/api/operation-history"

# ЮMoney удерживает комиссию с получателя (примерно 1% за кошелёк, 3% за карту),
# поэтому на кошелёк приходит чуть меньше цены. Допускаем недостачу до 10%.
MIN_AMOUNT_RATIO = 0.90


def wallet() -> str:
    return os.getenv("YOOMONEY_WALLET", "").strip()


def token() -> str:
    return os.getenv("YOOMONEY_TOKEN", "").strip()


def enabled() -> bool:
    return bool(wallet() and token())


# ─── Метки ──────────────────────────────────────────────────────────────────

def make_label(plan_key: str, user_id: int) -> str:
    """Уникальная метка платежа (до 64 символов)."""
    return f"wf:{plan_key}:{user_id}:{secrets.token_hex(3)}"


def parse_label(label: str) -> tuple[str, int] | None:
    """'wf:month:123:abc' → ('month', 123). None для чужих меток."""
    try:
        prefix, plan_key, uid, _ = (label or "").split(":", 3)
        if prefix != "wf":
            return None
        return plan_key, int(uid)
    except ValueError:
        return None


# ─── Ссылка на оплату ───────────────────────────────────────────────────────

def quickpay_params(label: str, amount_rub: int, title: str, payment_type: str = "AC") -> dict:
    """Параметры формы Quickpay. payment_type: AC — банковская карта, PC — кошелёк ЮMoney."""
    return {
        "receiver": wallet(),
        "quickpay-form": "button",
        "paymentType": payment_type,
        "sum": f"{amount_rub:.2f}",
        "label": label,
        "targets": title[:150],
        "successURL": os.getenv("YOOMONEY_SUCCESS_URL", "https://t.me/"),
    }


async def create_payment_url(label: str, amount_rub: int, title: str, payment_type: str = "AC") -> str:
    """
    Форма Quickpay принимает только POST. Отправляем POST и берём адрес,
    на который ЮMoney перенаправляет, — это и есть ссылка на страницу оплаты.
    """
    import httpx

    params = quickpay_params(label, amount_rub, title, payment_type)
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        r = await client.post(QUICKPAY_URL, data=params)
    location = r.headers.get("location")
    if r.status_code in (301, 302, 303, 307, 308) and location:
        return location if location.startswith("http") else API_BASE + location
    raise RuntimeError(f"ЮMoney не вернул ссылку на оплату (HTTP {r.status_code})")


# ─── Проверка платежей ──────────────────────────────────────────────────────

async def fetch_incoming(since: datetime, records: int = 100) -> list[dict]:
    """Входящие переводы с момента since (новые первыми)."""
    import httpx

    data = {
        "type": "deposition",
        "records": str(records),
        "from": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {"Authorization": f"Bearer {token()}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(HISTORY_URL, data=data, headers=headers)
    if r.status_code == 401:
        raise RuntimeError("ЮMoney: токен недействителен (401). Получи новый YOOMONEY_TOKEN.")
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(f"ЮMoney: ошибка API: {payload['error']}")
    return payload.get("operations", [])


def match_payments(operations: list[dict], pending: list[dict], prices_rub: dict[str, int]) -> list[dict]:
    """
    Сопоставляет операции из истории с ожидающими оплатами.
    pending: [{"label", "user_id", "plan"}]. Возвращает список найденных оплат:
    [{"label", "user_id", "plan", "operation_id", "amount"}].
    """
    by_label = {p["label"]: p for p in pending}
    found = []
    for op in operations:
        label = op.get("label")
        p = by_label.get(label)
        if not p:
            continue
        if op.get("status") != "success" or op.get("direction", "in") != "in":
            continue
        price = prices_rub.get(p["plan"])
        amount = float(op.get("amount", 0) or 0)
        if price is None or amount < price * MIN_AMOUNT_RATIO:
            logger.warning("ЮMoney: сумма %.2f по метке %s меньше ожидаемой %s", amount, label, price)
            continue
        found.append({
            "label": label,
            "user_id": p["user_id"],
            "plan": p["plan"],
            "operation_id": str(op.get("operation_id", label)),
            "amount": amount,
        })
    return found


def since_for(pending: list[dict]) -> datetime:
    """С какого момента запрашивать историю: самая старая ожидающая оплата минус 10 минут."""
    oldest = min(datetime.fromisoformat(p["created_at"]) for p in pending)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return oldest - timedelta(minutes=10)
