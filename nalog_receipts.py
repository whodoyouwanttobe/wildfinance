"""
nalog_receipts.py — чеки самозанятого через «Мой налог» (lknpd.nalog.ru).

Два режима:
  • АВТО — заданы NALOG_INN и NALOG_PASSWORD (пароль от личного кабинета
    налогоплательщика / lknpd.nalog.ru). Бот сам регистрирует доход и сразу
    отдаёт покупателю ссылку на чек.
    ⚠️ Это неофициальный API веб-кабинета «Мой налог» (тот же, через который
    работает сайт lknpd.nalog.ru). ФНС может его изменить — тогда бот
    автоматически переключится на ручной режим и напишет админу.
  • РУЧНОЙ — авто не настроено или не сработало. Бот присылает админу
    готовые данные для чека; админ создаёт чек в приложении «Мой налог»,
    копирует ссылку и отправляет боту: /receipt <номер> <ссылка>.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

API = "https://lknpd.nalog.ru/api/v1"
MSK = timezone(timedelta(hours=3))

SERVICE_NAMES = {
    "month": "Информационно-аналитические услуги WildFinance (доступ на 1 месяц)",
    "year": "Информационно-аналитические услуги WildFinance (доступ на 12 месяцев)",
    "forever": "Информационно-аналитические услуги WildFinance (бессрочный доступ)",
}

RECEIPT_URL_RE = re.compile(r"^https://lknpd\.nalog\.ru/api/v1/receipt/\d{10,12}/[A-Za-z0-9]+/print$")


def service_name(plan_key: str) -> str:
    return SERVICE_NAMES.get(plan_key, SERVICE_NAMES["month"])


def auto_enabled() -> bool:
    return bool(os.getenv("NALOG_INN", "").strip() and os.getenv("NALOG_PASSWORD", ""))


def receipt_url(inn: str, receipt_uuid: str) -> str:
    return f"{API}/receipt/{inn}/{receipt_uuid}/print"


def looks_like_receipt_url(text: str) -> bool:
    """Ссылка на чек из «Мой налог» (поделиться → ссылка)."""
    text = text.strip()
    return bool(RECEIPT_URL_RE.match(text)) or text.startswith("https://lknpd.nalog.ru/")


def _iso(dt: datetime) -> str:
    return dt.astimezone(MSK).isoformat(timespec="seconds")


def build_income_payload(plan_key: str, amount_rub: float, paid_at: datetime | None = None) -> dict:
    """Тело запроса регистрации дохода (вынесено отдельно для тестов)."""
    now = datetime.now(MSK)
    paid_at = paid_at or now
    amount = round(float(amount_rub), 2)
    return {
        "operationTime": _iso(paid_at),
        "requestTime": _iso(now),
        "services": [{"name": service_name(plan_key), "amount": amount, "quantity": 1}],
        "totalAmount": f"{amount:.2f}",
        "client": {"contactPhone": None, "displayName": None, "inn": None, "incomeType": "FROM_INDIVIDUAL"},
        "paymentType": os.getenv("NALOG_PAYMENT_TYPE", "CASH"),
        "ignoreMaxTotalIncomeRestriction": False,
    }


class MoyNalog:
    """Минимальный клиент веб-API «Мой налог»: вход, обновление токена, чек."""

    def __init__(self, inn: str, password: str, state_path: str):
        self.inn = inn
        self.password = password
        self.state_path = state_path
        self.state = self._load()
        if not self.state.get("device_id"):
            self.state["device_id"] = uuid.uuid4().hex[:21]

    # ── состояние (токены переживают перезапуск, чтобы не логиниться каждый раз)
    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)
        try:
            os.chmod(self.state_path, 0o600)
        except OSError:
            pass

    def _device(self) -> dict:
        return {
            "sourceDeviceId": self.state["device_id"],
            "sourceType": "WEB",
            "appVersion": "1.0.0",
            "metaDetails": {"userAgent": "Mozilla/5.0 (WildFinance bot)"},
        }

    async def _auth(self, client) -> None:
        if self.state.get("refresh_token"):
            r = await client.post(f"{API}/auth/token", json={
                "deviceInfo": self._device(), "refreshToken": self.state["refresh_token"],
            })
            if r.status_code == 200 and r.json().get("token"):
                self._remember(r.json())
                return
        r = await client.post(f"{API}/auth/lkfl", json={
            "username": self.inn, "password": self.password, "deviceInfo": self._device(),
        })
        if r.status_code != 200:
            raise RuntimeError(f"Мой налог: вход не удался ({r.status_code}): {r.text[:200]}")
        self._remember(r.json())

    def _remember(self, data: dict) -> None:
        self.state["token"] = data["token"]
        if data.get("refreshToken"):
            self.state["refresh_token"] = data["refreshToken"]
        self.state["token_expire"] = data.get("tokenExpireIn", "")
        self._save()

    def _token_fresh(self) -> bool:
        exp = self.state.get("token_expire")
        if not (self.state.get("token") and exp):
            return False
        try:
            when = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            return False
        return when - datetime.now(timezone.utc) > timedelta(minutes=2)

    async def create_receipt(self, plan_key: str, amount_rub: float, paid_at: datetime | None = None) -> tuple[str, str]:
        """Регистрирует доход. Возвращает (uuid чека, ссылка на чек)."""
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            if not self._token_fresh():
                await self._auth(client)
            payload = build_income_payload(plan_key, amount_rub, paid_at)
            for attempt in range(2):
                r = await client.post(
                    f"{API}/income", json=payload,
                    headers={"Authorization": f"Bearer {self.state['token']}"},
                )
                if r.status_code == 401 and attempt == 0:
                    await self._auth(client)
                    continue
                break
            if r.status_code != 200:
                raise RuntimeError(f"Мой налог: чек не создан ({r.status_code}): {r.text[:200]}")
            receipt_uuid = r.json().get("approvedReceiptUuid")
            if not receipt_uuid:
                raise RuntimeError(f"Мой налог: в ответе нет номера чека: {r.text[:200]}")
            return receipt_uuid, receipt_url(self.inn, receipt_uuid)


_client: MoyNalog | None = None


def get_client() -> MoyNalog | None:
    global _client
    if not auto_enabled():
        return None
    if _client is None:
        data_dir = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
        _client = MoyNalog(
            os.getenv("NALOG_INN", "").strip(),
            os.getenv("NALOG_PASSWORD", ""),
            os.path.join(data_dir, "nalog_session.json"),
        )
    return _client


def manual_instructions(receipt_no: int, plan_key: str, amount_rub: float, paid_at: str) -> str:
    """Текст админу: какой чек создать в «Мой налог» и как вернуть ссылку боту."""
    return (
        f"🧾 <b>Нужен чек №{receipt_no}</b>\n\n"
        "«Мой налог» → Новая продажа:\n"
        f"• Услуга: <code>{service_name(plan_key)}</code>\n"
        f"• Сумма: <code>{amount_rub:.2f}</code>\n"
        "• Покупатель: физическое лицо\n"
        f"• Дата оплаты: {paid_at}\n\n"
        "Потом «Поделиться» → скопировать ссылку и отправить мне:\n"
        f"<code>/receipt {receipt_no} </code>ссылка"
    )
