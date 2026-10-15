"""
mp_api.py — загрузка финансовых отчётов Wildberries и Ozon по API продавца.

Идея: по API получаем те же строки, что продавец скачивает в кабинете, и собираем
из них Excel с теми же названиями столбцов. Дальше работает обычный разбор
(parser_dispatcher.analyze_full), штрафы, история, ABC/XYZ — без отдельной логики.

Wildberries (документация: dev.wildberries.ru, категория «Финансы»):
    POST https://finance-api.wildberries.ru/api/finance/v1/sales-reports/detailed
    Заголовок Authorization: <токен>. Токен категории «Финансы», «Только на чтение».
    Пагинация по rrdId, конец — HTTP 204. Лимит: 1 запрос/мин (базовый токен — 2 в сутки).
    Старый GET statistics-api …/api/v5/supplier/reportDetailByPeriod WB отключает с 15.07.2026 —
    используется только как запасной вариант.

Ozon (docs.ozon.ru/api/seller):
    POST https://api-seller.ozon.ru/v1/finance/accrual/by-day  {"date": "YYYY-MM-DD", "last_id": ""}
    Заголовки Client-Id, Api-Key. Ключ с ролью «Admin read only».
    Старые /v3/finance/transaction/list|totals отключены 08.09.2026.
    Названия начислений — /v1/finance/accrual/types, артикулы — /v3/product/info/list.

Ключи хранятся в БД зашифрованными (Fernet). Ключ шифрования — API_ENC_KEY в .env;
без него подключение кабинета выключено.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd

logger = logging.getLogger(__name__)

WB_FINANCE = "https://finance-api.wildberries.ru"
WB_STATISTICS = "https://statistics-api.wildberries.ru"
OZON = "https://api-seller.ozon.ru"

MSK = timezone(timedelta(hours=3))
TIMEOUT = 60


class ApiError(Exception):
    """Ошибка с текстом для пользователя. retry_after — через сколько секунд можно повторить."""

    def __init__(self, message: str, *, retry_after: int | None = None, auth: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.auth = auth          # ключ недействителен — повторять бессмысленно


class NoData(ApiError):
    """Отчёт за период ещё не сформирован маркетплейсом."""


# ─── Шифрование ключей ───────────────────────────────────────────────────────

def _fernet():
    key = os.getenv("API_ENC_KEY", "").strip()
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode())


def enabled() -> bool:
    try:
        return _fernet() is not None
    except Exception:
        logger.error("API_ENC_KEY задан неверно — нужен ключ Fernet (44 символа base64)")
        return False


def encrypt(data: dict) -> str:
    f = _fernet()
    if f is None:
        raise RuntimeError("API_ENC_KEY не задан")
    return f.encrypt(json.dumps(data).encode()).decode()


def decrypt(secret: str) -> dict:
    f = _fernet()
    if f is None:
        raise RuntimeError("API_ENC_KEY не задан")
    return json.loads(f.decrypt(secret.encode()).decode())


# ─── Периоды ─────────────────────────────────────────────────────────────────

def last_full_week(today: date | None = None) -> tuple[date, date]:
    """Прошлая неделя понедельник–воскресенье (по Москве)."""
    today = today or datetime.now(MSK).date()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def week_key(start: date) -> str:
    y, w, _ = start.isocalendar()
    return f"{y}-W{w:02d}"


def period_text(start: date, end: date) -> str:
    return f"{start:%d.%m}–{end:%d.%m.%Y}"


def _num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):                      # Ozon: {"amount": "56.07", "currency": "RUB"}
        return _num(v.get("amount") or v.get("value"))
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


def _client():
    import httpx
    return httpx.AsyncClient(timeout=TIMEOUT)


def _retry_after(resp, default: int = 60) -> int:
    for h in ("X-Ratelimit-Retry", "Retry-After", "X-Ratelimit-Reset"):
        v = resp.headers.get(h)
        if v:
            try:
                return max(1, int(float(v)))
            except ValueError:
                pass
    return default


# ═══════════════════════════════════════════════════════════════════════════════
# WILDBERRIES
# ═══════════════════════════════════════════════════════════════════════════════

# Биты поля "s" в JWT-токене WB (dev.wildberries.ru → «Информация об API»)
WB_READ_ONLY_BIT = 30


def wb_token_info(token: str) -> dict:
    """Читает публичную часть JWT (без проверки подписи): срок действия и «только чтение»."""
    info = {"exp": None, "read_only": None, "sandbox": False}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        if data.get("exp"):
            info["exp"] = datetime.fromtimestamp(int(data["exp"]), tz=timezone.utc)
        if "s" in data:
            info["read_only"] = bool((int(data["s"]) >> WB_READ_ONLY_BIT) & 1)
        info["sandbox"] = bool(data.get("t"))
    except Exception:
        pass
    return info


def looks_like_wb_token(text: str) -> bool:
    t = (text or "").strip()
    return t.count(".") == 2 and len(t) > 100 and " " not in t


async def wb_check(token: str) -> dict:
    """Проверяет токен (GET /ping на finance-api). Возвращает wb_token_info."""
    info = wb_token_info(token)
    if info["exp"] and info["exp"] < datetime.now(timezone.utc):
        raise ApiError("Срок действия токена истёк — создай новый в кабинете WB.", auth=True)
    async with _client() as c:
        r = await c.get(f"{WB_FINANCE}/ping", headers={"Authorization": token})
    if r.status_code == 401:
        raise ApiError("WB не принял токен. Проверь, что скопировал его целиком.", auth=True)
    if r.status_code == 403:
        raise ApiError("У токена нет доступа к категории «Финансы». Создай токен с галочкой «Финансы».",
                       auth=True)
    if r.status_code == 429:
        raise ApiError("WB просит подождать — попробуй через минуту.", retry_after=_retry_after(r))
    if r.status_code >= 400:
        raise ApiError(f"WB ответил ошибкой {r.status_code}. Попробуй позже.")
    return info


# Поле API → столбец Excel-детализации (формат, который понимает wb_parser)
WB_FIELDS = {
    "vendorCode": "Артикул поставщика",
    "sellerOperName": "Обоснование для оплаты",
    "docTypeName": "Тип документа",
    "forPay": "К перечислению Продавцу за реализованный товар",
    "deliveryService": "Услуги по доставке товара покупателю",
    "penalty": "Общая сумма штрафов",
    "paidStorage": "Хранение",
    "paidAcceptance": "Операции при приёмке",
    "deduction": "Удержания",
    "quantity": "Кол-во",
    "retailPriceWithDisc": "Цена розничная с учётом согласованной скидки",
    "retailPrice": "Цена розничная",
    "retailAmount": "Вайлдберриз реализовал товар (Пр)",
    "saleDt": "Дата продажи",
    "orderDt": "Дата заказа покупателем",
    "rrDate": "Дата операции",
    "srid": "Srid",
    "nmId": "Код номенклатуры",
    "sku": "Баркод",
    "shkId": "ШК",
    "subjectName": "Предмет",
    "title": "Название",
    "brandName": "Бренд",
    "bonusTypeName": "Виды доставок, штрафов и корректировок ВВ",
    "orderId": "Номер сборочного задания",
    "additionalPayment": "Корректировка Вознаграждения Вайлдберриз (ВВ)",
    "rebillLogisticCost": "Возмещение издержек по перевозке/по складским операциям с товаром",
    "paymentSchedule": "Разовое изменение срока перечисления денежных средств",
    "deliveryAmount": "Количество доставок",
    "returnAmount": "Количество возврата",
    "officeName": "Склад",
    "reportId": "№ отчёта",
}
# Старый метод v5 (snake_case) → те же имена, что у нового
WB_LEGACY = {
    "sa_name": "vendorCode", "supplier_oper_name": "sellerOperName", "doc_type_name": "docTypeName",
    "ppvz_for_pay": "forPay", "delivery_rub": "deliveryService", "penalty": "penalty",
    "storage_fee": "paidStorage", "acceptance": "paidAcceptance", "deduction": "deduction",
    "quantity": "quantity", "retail_price_withdisc_rub": "retailPriceWithDisc",
    "retail_price": "retailPrice", "retail_amount": "retailAmount", "sale_dt": "saleDt",
    "order_dt": "orderDt", "rr_dt": "rrDate", "srid": "srid", "nm_id": "nmId", "barcode": "sku",
    "shk_id": "shkId", "subject_name": "subjectName", "brand_name": "brandName",
    "bonus_type_name": "bonusTypeName", "assembly_id": "orderId",
    "additional_payment": "additionalPayment", "rebill_logistic_cost": "rebillLogisticCost",
    "payment_schedule": "paymentSchedule", "delivery_amount": "deliveryAmount",
    "return_amount": "returnAmount", "office_name": "officeName",
    "realizationreport_id": "reportId", "rrd_id": "rrdId",
}
WB_MONEY = {"forPay", "deliveryService", "penalty", "paidStorage", "paidAcceptance", "deduction",
            "retailPriceWithDisc", "retailPrice", "retailAmount", "additionalPayment",
            "rebillLogisticCost", "paymentSchedule"}
WB_DATES = {"saleDt", "orderDt", "rrDate"}
WB_PAGE = 100_000


async def _wb_detailed(c, token: str, start: date, end: date) -> list[dict]:
    rows: list[dict] = []
    rrd = 0
    while True:
        body = {"dateFrom": start.isoformat(), "dateTo": end.isoformat(),
                "limit": WB_PAGE, "rrdId": rrd, "period": "weekly"}
        r = await c.post(f"{WB_FINANCE}/api/finance/v1/sales-reports/detailed",
                         headers={"Authorization": token}, json=body)
        if r.status_code == 204:
            break
        if r.status_code == 404:
            raise FileNotFoundError("new method missing")
        _wb_raise(r)
        batch = r.json() or []
        if isinstance(batch, dict):                       # на случай обёртки {"data": [...]}
            batch = batch.get("data") or batch.get("result") or []
        if not batch:
            break
        rows.extend(batch)
        new_rrd = max(int(_num(x.get("rrdId"))) for x in batch)
        if len(batch) < WB_PAGE or new_rrd <= rrd:
            break
        rrd = new_rrd
        await asyncio.sleep(61)                           # лимит WB: 1 запрос в минуту
    return rows


async def _wb_legacy(c, token: str, start: date, end: date) -> list[dict]:
    rows: list[dict] = []
    rrd = 0
    while True:
        r = await c.get(f"{WB_STATISTICS}/api/v5/supplier/reportDetailByPeriod",
                        headers={"Authorization": token},
                        params={"dateFrom": start.isoformat(), "dateTo": end.isoformat(),
                                "limit": WB_PAGE, "rrdid": rrd, "period": "weekly"})
        if r.status_code == 204:
            break
        _wb_raise(r)
        batch = r.json() or []
        if not batch:
            break
        rows.extend({WB_LEGACY.get(k, k): v for k, v in x.items()} for x in batch)
        new_rrd = max(int(_num(x.get("rrd_id"))) for x in batch)
        if len(batch) < WB_PAGE or new_rrd <= rrd:
            break
        rrd = new_rrd
        await asyncio.sleep(61)
    return rows


def _wb_raise(r) -> None:
    if r.status_code < 400:
        return
    if r.status_code == 401:
        raise ApiError("WB не принял токен — возможно, он удалён или истёк. Подключи заново: /connect",
                       auth=True)
    if r.status_code == 403:
        raise ApiError("У токена нет доступа к категории «Финансы». Создай новый токен: /connect", auth=True)
    if r.status_code == 429:
        sec = _retry_after(r)
        raise ApiError(f"WB ограничивает частоту запросов. Повторить можно через {_human(sec)}.",
                       retry_after=sec)
    raise ApiError(f"WB ответил ошибкой {r.status_code}. Попробуй позже.")


def _human(sec: int) -> str:
    if sec < 90:
        return f"{sec} сек"
    if sec < 5400:
        return f"{round(sec / 60)} мин"
    return f"{round(sec / 3600)} ч"


async def wb_fetch(token: str, start: date, end: date) -> list[dict]:
    async with _client() as c:
        try:
            rows = await _wb_detailed(c, token, start, end)
        except FileNotFoundError:
            logger.warning("WB: новый метод sales-reports/detailed не найден, пробую v5")
            rows = await _wb_legacy(c, token, start, end)
    if not rows:
        raise NoData(f"WB ещё не сформировал отчёт за {period_text(start, end)}. "
                     "Обычно он появляется в понедельник–вторник после окончания недели.")
    return rows


def wb_rows_to_df(rows: list[dict]) -> pd.DataFrame:
    recs = []
    for x in rows:
        rec = {}
        for field, col in WB_FIELDS.items():
            v = x.get(field)
            if field in WB_MONEY:
                v = _num(v)
            elif field == "quantity" or field in ("deliveryAmount", "returnAmount"):
                v = int(_num(v))
            elif field in WB_DATES:
                v = str(v or "")[:10]
            else:
                v = "" if v is None else v
            rec[col] = v
        recs.append(rec)
    return pd.DataFrame(recs, columns=list(WB_FIELDS.values()))


# ═══════════════════════════════════════════════════════════════════════════════
# OZON
# ═══════════════════════════════════════════════════════════════════════════════

OZON_BY_DAY = "/v1/finance/accrual/by-day"


def parse_ozon_credentials(text: str) -> tuple[str, str] | None:
    """«Client ID» и «API-ключ» из одного сообщения (в любом порядке, через пробел или с новой строки)."""
    parts = [p.strip(" :;,") for p in (text or "").replace("\n", " ").split()]
    client_id = next((p for p in parts if p.isdigit() and 3 <= len(p) <= 12), None)
    key = next((p for p in parts if len(p) >= 30 and not p.isdigit()), None)
    if client_id and key:
        return client_id, key
    return None


def _oz_headers(client_id: str, api_key: str) -> dict:
    return {"Client-Id": client_id, "Api-Key": api_key, "Content-Type": "application/json"}


def _oz_raise(r) -> None:
    if r.status_code < 400:
        return
    if r.status_code in (401, 403):
        raise ApiError("Ozon не принял ключ — проверь Client ID и API-ключ или создай новый: /connect",
                       auth=True)
    if r.status_code == 429:
        sec = _retry_after(r, 5)
        raise ApiError(f"Ozon просит подождать {_human(sec)}.", retry_after=sec)
    try:
        msg = (r.json() or {}).get("message", "")
    except Exception:
        msg = ""
    raise ApiError(f"Ozon ответил ошибкой {r.status_code}{': ' + msg if msg else ''}. Попробуй позже.")


async def ozon_check(client_id: str, api_key: str) -> dict:
    """Проверяет ключ через /v1/roles: доступ к начислениям и срок действия."""
    async with _client() as c:
        r = await c.post(f"{OZON}/v1/roles", headers=_oz_headers(client_id, api_key), json={})
        if r.status_code == 404:                    # метода нет — проверяем просто запросом справочника
            r2 = await c.post(f"{OZON}/v1/finance/accrual/types", headers=_oz_headers(client_id, api_key), json={})
            _oz_raise(r2)
            return {"expires_at": None, "roles": []}
        _oz_raise(r)
        data = r.json() or {}
    roles = data.get("roles") or []
    methods = {m for role in roles for m in (role.get("methods") or [])}
    if roles and methods and OZON_BY_DAY not in methods:
        raise ApiError("У ключа нет доступа к финансовым начислениям. Создай ключ с ролью "
                       "«Admin read only» (только чтение): /connect", auth=True)
    exp = data.get("expires_at")
    return {"expires_at": exp, "roles": [r.get("name") for r in roles]}


async def _oz_post(c, path: str, client_id: str, api_key: str, body: dict) -> dict:
    for attempt in range(4):
        r = await c.post(f"{OZON}{path}", headers=_oz_headers(client_id, api_key), json=body)
        if r.status_code == 429 and attempt < 3:
            await asyncio.sleep(min(_retry_after(r, 2), 10))
            continue
        _oz_raise(r)
        return r.json() or {}
    return {}


# Группа услуг по английскому имени типа начисления. Названия групп подобраны так,
# чтобы ozon_parser.categorize() разложил строки по тем же статьям, что и Excel из кабинета.
_OZ_GROUP_RULES = (
    (("salecommission", "commission"), "Вознаграждение Ozon"),
    (("acquiring",), "Эквайринг"),
    (("fine", "penalt"), "Штрафы"),
    (("logistic", "delivery", "lastmile", "pickuppoint", "dropoff", "drop_off", "crossdock", "return",
      "pickup", "shipment", "courier"), "Логистика"),
    (("packag", "insurance", "star"), "Услуги партнёров"),
    (("promotion", "stencil", "click", "ads", "advert", "review", "pointsfordiscount", "brandpromo"),
     "Продвижение и реклама"),
)


def _oz_group(name: str) -> str:
    n = (name or "").lower()
    for keys, group in _OZ_GROUP_RULES:
        if any(k in n for k in keys):
            return group
    return "Другие услуги и штрафы"


def _oz_type_text(type_id, types: dict) -> tuple[str, str]:
    """(Тип начисления по-русски, Группа услуг) по type_id из справочника."""
    name, desc = types.get(str(type_id), ("", ""))
    text = (desc or name or f"Начисление #{type_id}").strip()
    if ("fine" in name.lower() or "penalt" in name.lower()) and "штраф" not in text.lower():
        text = f"Штраф: {text}"
    return text, _oz_group(name)


def _schema(v) -> str:
    v = str(v or "").strip()
    return v.upper() if v else ""


def ozon_accruals_to_rows(accruals: list[dict], types: dict) -> list[dict]:
    """Начисления by-day → строки «Детализации начислений»."""
    out: list[dict] = []

    def row(acc, sku, type_text, group, amount, schema=""):
        out.append({
            "ID начисления": str(acc.get("accrual_id") or acc.get("id") or ""),
            "Дата начисления": str(acc.get("date") or "")[:10],
            "Группа услуг": group,
            "Тип начисления": type_text,
            "Сумма итого, руб": round(amount, 2),
            "SKU": str(sku or ""),
            "Схема работы": schema,
            "Номер отправления": str(acc.get("unit_number") or ""),
        })

    for acc in accruals:
        total = _num(acc.get("total_amount"))
        before = len(out)
        posting = acc.get("posting") or {}
        schema = _schema(posting.get("delivery_schema"))
        for p in posting.get("products") or []:
            sku = p.get("sku")
            com = p.get("commission") or {}
            sale = _num(com.get("sale_amount"))
            if sale:
                if sale > 0:
                    row(acc, sku, "Выручка", "Продажи", sale, schema)
                else:
                    row(acc, sku, "Возврат выручки", "Возвраты", sale, schema)
            fee = _num(com.get("sale_commission"))
            if fee:
                row(acc, sku, "Вознаграждение за продажу" if fee < 0 else "Возврат вознаграждения",
                    "Вознаграждение Ozon", fee, schema)
            for s in ((p.get("delivery") or {}).get("services") or []):
                t, g = _oz_type_text(s.get("type_id", s.get("accrual_id")), types)
                row(acc, sku, t, g, _num(s.get("accrued")), schema)
        for item in ((acc.get("item_fees") or {}).get("fees") or []):
            for f in item.get("fees") or []:
                t, g = _oz_type_text(f.get("type_id", f.get("accrual_id")), types)
                row(acc, item.get("sku"), t, g, _num(f.get("accrued")))
        nf = acc.get("non_item_fee")
        if nf:
            t, g = _oz_type_text(nf.get("type_id", nf.get("accrual_id")), types)
            row(acc, "", t, g, _num(nf.get("accrued")))
        cf = acc.get("container_fees") or {}
        for f in (cf.get("fees") if isinstance(cf, dict) else cf) or []:
            t, g = _oz_type_text(f.get("type_id", f.get("accrual_id")), types)
            row(acc, "", t, g, _num(f.get("accrued")))
        # Сверка: сумма строк должна совпасть с total_amount начисления
        got = sum(r["Сумма итого, руб"] for r in out[before:])
        diff = round(total - got, 2)
        if abs(diff) >= 0.01:
            row(acc, "", "Прочие начисления", "Другие услуги и штрафы", diff, schema)
    return out


async def _oz_types(c, client_id: str, api_key: str) -> dict:
    try:
        data = await _oz_post(c, "/v1/finance/accrual/types", client_id, api_key, {})
    except ApiError:
        logger.exception("Ozon: справочник начислений недоступен")
        return {}
    items = data.get("accrual_types") or data.get("types") or data.get("result") or []
    return {str(t.get("id")): (t.get("name") or "", t.get("description") or "") for t in items}


async def _oz_products(c, client_id: str, api_key: str, skus: list[str]) -> dict:
    """SKU → (артикул продавца, название)."""
    out: dict = {}
    ids = [s for s in skus if s and s.isdigit()]
    for i in range(0, len(ids), 100):
        try:
            data = await _oz_post(c, "/v3/product/info/list", client_id, api_key,
                                  {"sku": [int(s) for s in ids[i:i + 100]]})
        except ApiError:
            logger.warning("Ozon: не удалось получить артикулы товаров")
            break
        items = data.get("items") or (data.get("result") or {}).get("items") or []
        for it in items:
            name, offer = it.get("name") or "", it.get("offer_id") or ""
            for key in ("sku", "fbo_sku", "fbs_sku"):
                if it.get(key):
                    out[str(it[key])] = (offer, name)
            for src in it.get("sources") or []:
                if src.get("sku"):
                    out[str(src["sku"])] = (offer, name)
    return out


async def ozon_fetch(client_id: str, api_key: str, start: date, end: date) -> list[dict]:
    accruals: list[dict] = []
    async with _client() as c:
        types = await _oz_types(c, client_id, api_key)
        day = start
        while day <= end:
            last_id = ""
            for _ in range(500):                         # защита от бесконечной пагинации
                data = await _oz_post(c, OZON_BY_DAY, client_id, api_key,
                                      {"date": day.isoformat(), "last_id": last_id})
                batch = data.get("accruals") or (data.get("result") or {}).get("accruals") or []
                accruals.extend(batch)
                new_last = str(data.get("last_id") or "")
                if not batch or not new_last or new_last == last_id:
                    break
                last_id = new_last
                await asyncio.sleep(0.2)
            day += timedelta(days=1)
        rows = ozon_accruals_to_rows(accruals, types)
        products = await _oz_products(c, client_id, api_key, sorted({r["SKU"] for r in rows if r["SKU"]}))
    for r in rows:
        offer, name = products.get(r["SKU"], ("", ""))
        r["Артикул"] = offer
        r["Название товара"] = name
    if not rows:
        raise NoData(f"Ozon не вернул начислений за {period_text(start, end)}.")
    return rows


OZON_COLUMNS = ["ID начисления", "Дата начисления", "Группа услуг", "Тип начисления", "Артикул", "SKU",
                "Название товара", "Схема работы", "Номер отправления", "Сумма итого, руб"]


def ozon_rows_to_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=OZON_COLUMNS)


# ═══════════════════════════════════════════════════════════════════════════════
# Общее
# ═══════════════════════════════════════════════════════════════════════════════

def to_xlsx(df: pd.DataFrame, prefix: str) -> str:
    """Сохраняет таблицу во временный Excel (удалить после разбора!)."""
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix=prefix)
    os.close(fd)
    df.to_excel(path, index=False)
    return path


async def fetch_report_file(marketplace: str, creds: dict, start: date, end: date) -> str:
    """Скачивает отчёт за период и возвращает путь к временному xlsx в формате кабинета."""
    if marketplace == "WB":
        rows = await wb_fetch(creds["token"], start, end)
        return to_xlsx(wb_rows_to_df(rows), "wb_api_")
    if marketplace == "Ozon":
        rows = await ozon_fetch(creds["client_id"], creds["api_key"], start, end)
        return to_xlsx(ozon_rows_to_df(rows), "ozon_api_")
    raise ValueError(marketplace)
