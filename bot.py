"""
bot.py — Асинхронный Telegram-бот на aiogram 3.x.

Экспресс-аудитор отчётов Wildberries и Ozon.

Функции:
  - /start — приветствие + регистрация
  - Загрузка .xlsx/.csv → финансовый анализ (HTML с эмодзи) + ABC-анализ
  - «📊 Получить пример отчёта» — демо-файл WB/Ozon
  - /compare — сравнение двух отчётов («было → стало») + AI-выводы
  - /buy — оплата через Telegram Payments (ЮKassa) с автовыдачей доступа
  - /give_access <user_id> <дней> — админ-команда (только для ADMIN_ID)
  - Обратная связь: звёзды + текст после первого отчёта

Переменные окружения:
  BOT_TOKEN              — токен Telegram-бота от @BotFather
  ADMIN_ID               — Telegram ID администратора (для /give_access)
  PAYMENT_PROVIDER_TOKEN — токен ЮKassa из @BotFather → Payments (автооплата).
                           Если не задан — показываются ссылки PAYMENT_URL_*.

Запуск:
  python bot.py
"""

import os
import re
import json
import tempfile
from datetime import date, datetime, timedelta, timezone
import asyncio
import logging
import traceback
from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F, types, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    PreCheckoutQuery,
    LabeledPrice,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
)

from database import (
    add_user,
    is_trial_active,
    grant_access,
    increment_report_count,
    save_feedback,
    has_given_feedback,
    get_all_users_stats,
    get_user,
    extend_access,
    record_payment,
    get_payments_total,
    set_user_source,
    get_source_stats,
    set_costs,
    get_costs,
    delete_cost,
    get_tax,
    set_tax,
    save_last_report,
    get_last_report,
    add_pending_payment,
    get_pending_payments,
    mark_pending_paid,
    add_receipt,
    set_receipt_url,
    get_user_receipts,
    get_pending_receipts,
    delete_user_data,
)
import yoomoney_pay as ym
import legal
import nalog_receipts as NR
from wb_parser import analyze as wb_analyze
from ozon_parser import analyze as ozon_analyze
from parser_dispatcher import analyze, analyze_full, detect_marketplace, compute
import profit as P
from report_compare import compare_metrics, compare_plain, compare_marketplaces
from ai_client import ai_summarize_report, ai_compare_reports, ai_chat, get_ai_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфигурация ────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_URL_MONTH = os.getenv("PAYMENT_URL_MONTH", "https://yookassa.ru")
PAYMENT_URL_FOREVER = os.getenv("PAYMENT_URL_FOREVER", "https://yookassa.ru")
CARD_DETAILS = os.getenv("CARD_DETAILS", "Сбербанк: 0000 0000 0000 0000 (Имя Ф.)")

# ─── Автооплата: Telegram Payments + ЮKassa ──────────────────────────────────
# Токен берётся в @BotFather → /mybots → Payments → ЮKassa.
# Оплата проходит внутри Telegram, бот получает successful_payment и сам
# выдаёт доступ — без вебхук-сервера и ручного /give_access.
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")
PRICE_MONTH_RUB = int(os.getenv("PRICE_MONTH_RUB", "1490"))
# Стартовая акция: «навсегда» продаётся только до FOREVER_PROMO_UNTIL (включительно, по Москве).
# После этой даты вместо «навсегда» показывается тариф на 12 месяцев (PRICE_YEAR_RUB).
# Пустой FOREVER_PROMO_UNTIL — «навсегда» доступен без ограничения по сроку.
PRICE_FOREVER_RUB = int(os.getenv("PRICE_FOREVER_RUB", "4990"))
FOREVER_PROMO_UNTIL = os.getenv("FOREVER_PROMO_UNTIL", "2026-10-11").strip()
PRICE_YEAR_RUB = int(os.getenv("PRICE_YEAR_RUB", "11900"))
PAYMENT_URL_YEAR = os.getenv("PAYMENT_URL_YEAR", "")
# Чек по 54-ФЗ через ЮKassa (нужен, если в ЮKassa подключена онлайн-касса / «Чеки от ЮKassa»)
YOOKASSA_SEND_RECEIPT = os.getenv("YOOKASSA_SEND_RECEIPT", "0") == "1"
YOOKASSA_VAT_CODE = int(os.getenv("YOOKASSA_VAT_CODE", "1"))  # 1 = без НДС

PLANS = {
    "month": {
        "title": "WildFinance — 1 месяц",
        "description": "Доступ к аудиту отчётов WB и Ozon на 30 дней.",
        "price_rub": PRICE_MONTH_RUB,
        "days": 30,
    },
    "forever": {
        "title": "WildFinance — навсегда",
        "description": "Бессрочный доступ к аудиту отчётов WB и Ozon.",
        "price_rub": PRICE_FOREVER_RUB,
        "days": 36500,
    },
    "year": {
        "title": "WildFinance — 12 месяцев",
        "description": "Доступ к аудиту отчётов WB и Ozon на 12 месяцев.",
        "price_rub": PRICE_YEAR_RUB,
        "days": 365,
    },
}
PLAN_KEYS = tuple(PLANS)

# ─── Тарифы и акция ──────────────────────────────────────────────────────────
MSK = timezone(timedelta(hours=3))


def _now_msk() -> datetime:
    return datetime.now(MSK)


def promo_deadline() -> date | None:
    """Последний день акции «навсегда» (по Москве) или None, если срока нет."""
    if not FOREVER_PROMO_UNTIL:
        return None
    try:
        return date.fromisoformat(FOREVER_PROMO_UNTIL)
    except ValueError:
        logger.error("FOREVER_PROMO_UNTIL=%r — неверная дата, нужен формат ГГГГ-ММ-ДД", FOREVER_PROMO_UNTIL)
        return None


def forever_available(now: datetime | None = None) -> bool:
    deadline = promo_deadline()
    if deadline is None:
        return True
    return (now or _now_msk()).astimezone(MSK).date() <= deadline


def promo_days_left(now: datetime | None = None) -> int | None:
    """Сколько полных дней акции осталось после сегодняшнего (0 — сегодня последний день)."""
    deadline = promo_deadline()
    if deadline is None:
        return None
    return (deadline - (now or _now_msk()).astimezone(MSK).date()).days


def days_word(n: int) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря"]


def promo_left_text(now: datetime | None = None) -> str:
    """«до 11 октября — осталось 11 дней» / «до 11 октября — последний день!»"""
    deadline = promo_deadline()
    left = promo_days_left(now)
    if deadline is None or left is None:
        return ""
    when = f"до {deadline.day} {_MONTHS_GEN[deadline.month - 1]}"
    if left <= 0:
        return f"{when} — сегодня последний день!"
    return f"{when} — осталось {left} {days_word(left)}"


def available_plans(now: datetime | None = None) -> list[str]:
    """Тарифы, которые сейчас продаются: месяц + (навсегда по акции | 12 месяцев)."""
    return ["month", "forever"] if forever_available(now) else ["month", "year"]


def plan_button_text(plan_key: str, now: datetime | None = None) -> str:
    price = PLANS[plan_key]["price_rub"]
    if plan_key == "month":
        return f"💳 1 месяц — {price} ₽"
    if plan_key == "forever":
        left = promo_days_left(now)
        tail = "" if left is None else (" · последний день" if left <= 0 else f" · ещё {left} {days_word(left)}")
        return f"🔥 Навсегда — {price} ₽{tail}"
    return f"📅 12 месяцев — {price} ₽"


def tariffs_text(now: datetime | None = None) -> str:
    """Описание тарифов для /buy и сообщения об окончании пробного периода."""
    m = PLANS["month"]["price_rub"]
    parts = [f"🔹 <b>Месяц — {m} ₽</b>\n   30 дней доступа. Без автосписаний — продлеваешь сам."]
    if forever_available(now):
        f = PLANS["forever"]["price_rub"]
        promo = promo_left_text(now)
        parts.append(
            f"🔥 <b>Навсегда — {f} ₽</b> (стартовая акция)\n"
            "   Один платёж — доступ без срока и все обновления."
            + (f"\n   ⏳ <b>Только {promo}.</b>" if promo else "")
        )
    else:
        y = PLANS["year"]["price_rub"]
        save = m * 12 - y
        per_month = round(y / 12)
        parts.append(
            f"📅 <b>12 месяцев — {y} ₽</b> (≈{per_month} ₽/мес)"
            + (f"\n   Выгода {save} ₽ по сравнению с помесячной оплатой." if save > 0 else "")
        )
    return "\n\n".join(parts)


def has_paid_access(user_id: int) -> bool:
    """Есть ли у пользователя действующий оплаченный доступ (не пробный период)."""
    u = get_user(user_id) or {}
    until = u.get("access_until")
    if not until:
        return False
    try:
        return datetime.fromisoformat(until) > datetime.utcnow()
    except ValueError:
        return False


def report_promo_footer(user_id: int, now: datetime | None = None) -> str:
    """Рекламный блок под отчётом — только для тех, кто ещё не оплатил."""
    if has_paid_access(user_id):
        return ""
    lines = ["", "➖➖➖➖➖➖➖➖➖➖➖➖"]
    u = get_user(user_id) or {}
    try:
        from database import FREE_TRIAL_DAYS
        trial_end = datetime.fromisoformat(u["join_date"]) + timedelta(days=FREE_TRIAL_DAYS)
        left = (trial_end - datetime.utcnow()).days
        if left >= 0:
            lines.append(f"🆓 Пробный период: осталось {left} {days_word(left)}." if left else "🆓 Пробный период заканчивается сегодня.")
    except (KeyError, TypeError, ValueError):
        pass
    lines.append(f"💎 Полный доступ — от {PLANS['month']['price_rub']} ₽/мес → /buy")
    if forever_available(now):
        promo = promo_left_text(now)
        lines.append(
            f"🔥 Навсегда за <b>{PLANS['forever']['price_rub']} ₽</b>"
            + (f" — только {promo}" if promo else "")
        )
    lines.append("➖➖➖➖➖➖➖➖➖➖➖➖")
    return "\n".join(lines)

# ─── Демо-отчёты для кнопки «Получить пример отчёта» ─────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_FILES = {
    "WB": os.path.join(_HERE, "wb_real_report.xlsx"),
    "Ozon": os.path.join(_HERE, "ozon_real_report.xlsx"),
}

if not BOT_TOKEN:
    raise RuntimeError(
        "Токен бота не задан. Установите переменную окружения BOT_TOKEN:\n"
        "  set BOT_TOKEN=123456:ABC-DEF  (Windows)\n"
        "  export BOT_TOKEN=123456:ABC-DEF  (Linux/Mac)"
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── Тексты сообщений ─────────────────────────────────────────────────────────

WELCOME_TEXT = (
    "👋 <b>Привет! Я экспресс-аудитор отчётов Wildberries и Ozon.</b>\n\n"
    "У тебя есть <b>7 дней бесплатного доступа</b>.\n\n"
    "📄 Отправь мне еженедельный детализированный отчет "
    "(Excel или CSV), и я за секунду посчитаю:\n\n"
    "  💰 Реальную чистую прибыль\n"
    "  🔴 Скрытые штрафы и переплаты\n"
    "  ⚠️ ТОП убыточных товаров\n"
    "  🔤 ABC-анализ ассортимента\n"
    "  💡 Рекомендации по оптимизации\n\n"
    "💡 <b>Хочешь сначала посмотреть, как это работает?</b>\n"
    "Нажми кнопку <b>«📊 Получить пример отчёта»</b> внизу —\n"
    "я пришлю готовый файл, который можно сразу загрузить обратно.\n\n"
    "Команды:\n"
    "  /compare — сравнить два отчёта (было → стало)\n"
    "  /buy — тарифы и оплата\n"
    "  /help — помощь\n\n"
    "<i>Пользуясь ботом, вы соглашаетесь с /terms и /privacy.</i>"
)

def trial_expired_text() -> str:
    return (
        "⏱ <b>Бесплатный период закончился.</b>\n\n"
        "Получи полный доступ:\n\n"
        + tariffs_text()
        + "\n\nВыбери тариф 👇"
    )

HELP_TEXT = (
    "📋 <b>Как пользоваться ботом:</b>\n\n"
    "1️⃣ Скачай детализированный отчёт:\n"
    "   • <b>Wildberries</b>: Личный кабинет → Финансы → Детализация\n"
    "   • <b>Ozon</b>: Финансы → Детализация начислений → выгрузи CSV/XLSX\n\n"
    "2️⃣ Пришли файл (.xlsx или .csv) в этот чат\n\n"
    "3️⃣ Получи полный анализ за секунды:\n"
    "   • Чистая прибыль по каждому артикулу\n"
    "   • Скрытые расходы и штрафы\n"
    "   • Рекомендации по оптимизации\n\n"
    "🤖 <b>Я сам определю</b>, какой маркетплейс — WB или Ozon.\n\n"
    "📌 Команды:\n"
    "  /compare — сравнить два отчёта (было → стало)\n"
    "  /wbozon — WB против Ozon: где выгоднее продавать\n"
    "  /cost — себестоимость товаров (для расчёта прибыли)\n"
    "  /tax — система налогообложения\n"
    "  /ai — AI-разбор последнего отчёта\n"
    "  /buy — тарифы и оплата\n"
    "  /receipts — мои чеки об оплате\n"
    "  /start — начать заново\n\n"
    "📄 Документы: /terms — оферта, /privacy — персональные данные, "
    "/delete_me — удалить мои данные"
)

SUPPORTED_EXTENSIONS = (".xlsx", ".xls", ".csv")

# Текст для пользователя, если AI-ключи не настроены (без технических деталей —
# админ видит причину в логах)
AI_UNAVAILABLE_TEXT = (
    "🤖 <b>AI-разбор временно недоступен.</b>\n\n"
    "Основной отчёт выше посчитан полностью — AI только добавляет выводы. "
    "Попробуй чуть позже."
)

# ─── Тексты ошибок ────────────────────────────────────────────────────────────

ERROR_WRONG_FORMAT = (
    "⚠️ Не могу прочитать этот файл.\n\n"
    "Убедись, что это детализированный отчёт:\n"
    "  • WB: Личный кабинет → Финансы → Детализация\n"
    "  • Ozon: Финансы → Детализация начислений\n\n"
    "Скачай файл ещё раз и пришли заново."
)

ERROR_EMPTY_FILE = (
    "📄 Файл пустой или не содержит данных за выбранный период.\n\n"
    "Попробуй выбрать другую неделю или другой отчёт."
)

ERROR_ENCODING = (
    "🔤 Проблема с кодировкой файла.\n\n"
    "Открой CSV в Excel, сохрани в формате .xlsx и пришли снова."
)

ERROR_TIMEOUT = (
    "⏱ Файл слишком большой, анализ занял много времени.\n\n"
    "Попробуй разбить отчёт на несколько недель и загрузить частями."
)

ERROR_GENERIC = (
    "🛠 Что-то пошло не так при анализе.\n\n"
    "Я уже получил уведомление об ошибке и разбираюсь.\n\n"
    "Попробуй ещё раз через 5 минут или пришли другой файл — "
    "возможно, этот отчёт содержит нестандартные данные."
)


def classify_error(exc: Exception) -> str:
    """Определяет понятное сообщение об ошибке по типу исключения."""
    msg = str(exc).lower()

    if isinstance(exc, ValueError):
        return ERROR_WRONG_FORMAT
    if isinstance(exc, UnicodeDecodeError):
        return ERROR_ENCODING

    if any(kw in msg for kw in ("excel", "openpyxl", "xlrd", "zipfile", "badzip")):
        return ERROR_WRONG_FORMAT
    if any(kw in msg for kw in ("empty", "no columns", "no rows", "shape")):
        return ERROR_EMPTY_FILE
    if "codec" in msg or "encoding" in msg or "decode" in msg:
        return ERROR_ENCODING
    if "timeout" in msg or "time limit" in msg:
        return ERROR_TIMEOUT

    return ERROR_GENERIC


# ═══════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════════

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню с кнопками внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Получить пример отчёта")],
            [KeyboardButton(text="📈 Моя статистика"), KeyboardButton(text="⚖️ WB против Ozon")],
            [
                KeyboardButton(text="💳 Оплатить доступ"),
                KeyboardButton(text="🎁 Пригласить друга"),
            ],
            [KeyboardButton(text="📄 Помощь")],
        ],
        resize_keyboard=True,
    )

def get_buy_keyboard(now: datetime | None = None) -> InlineKeyboardMarkup:
    """
    Клавиатура оплаты (тарифы — см. available_plans()).
    ЮMoney → Telegram Payments (ЮKassa) → простые ссылки, в порядке приоритета.
    """
    rows = []
    for key in available_plans(now):
        text = plan_button_text(key, now)
        if ym.enabled():
            rows.append([InlineKeyboardButton(text=text, callback_data=f"ym_{key}")])
        elif PAYMENT_PROVIDER_TOKEN:
            rows.append([InlineKeyboardButton(text=text, callback_data=f"buy_{key}")])
        else:
            url = {"month": PAYMENT_URL_MONTH, "forever": PAYMENT_URL_FOREVER, "year": PAYMENT_URL_YEAR}[key]
            if url:
                rows.append([InlineKeyboardButton(text=text, url=url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_demo_keyboard() -> InlineKeyboardMarkup:
    """Выбор демо-отчёта: WB или Ozon."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🟣 Wildberries", callback_data="demo_WB"),
        InlineKeyboardButton(text="🔵 Ozon", callback_data="demo_Ozon"),
    ]])


def get_compare_ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 AI-выводы по сравнению", callback_data="cmp_ai"),
    ]])


def get_feedback_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура выбора звёзд для отзыва.

    Используем Unicode-звёзды ★ (U+2605), которые узкие и точно помещаются
    на кнопке. Кнопка показывает «оценка + звёзды» в одну строку,
    например: "5 ★★★★★" — так все 5 звёзд видно и не обрезаются.
    """
    rows = []
    for i in range(1, 6):
        stars = "★" * i
        label = f"{i}  {stars}"
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"feedback_stars_{i}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="⏩ Пропустить", callback_data="feedback_skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_post_report_keyboard() -> InlineKeyboardMarkup:
    """
    Кнопки, которые показываются ПОД каждым отчётом:
      - ⭐ Оставить отзыв — сразу открывает клавиатуру выбора звёзд
      - 🚨 Сообщить о проблеме — переводит бота в режим ожидания жалобы,
        которая летит админу вместе с исходным файлом и traceback.
      - 🔄 Повторить анализ — перепарсить последний загруженный файл.
      - 🤖 AI-разбор — короткое саммари от LLM (нужен OPENAI_API_KEY).
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💰 Указать себестоимость → прибыль",
                callback_data="cost_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                text="⭐ Оставить отзыв",
                callback_data="post_review",
            ),
            InlineKeyboardButton(
                text="🚨 Сообщить о проблеме",
                callback_data="post_problem",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🤖 AI-разбор",
                callback_data="post_ai",
            ),
            InlineKeyboardButton(
                text="🔄 Повторить анализ",
                callback_data="post_repeat",
            ),
        ],
    ])


# ═══════════════════════════════════════════════════════════════════════════════
# ХЕНДЛЕРЫ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_start_source(text: str) -> str:
    """'/start wbchat' → 'wbchat'. Только латиница/цифры/_/-, до 32 символов."""
    parts = text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ""
    arg = parts[1].strip()
    return arg[:32] if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arg) else ""


@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start."""
    user_id = message.from_user.id
    username = message.from_user.username or ""

    is_new = add_user(user_id, username)
    source = _parse_start_source(message.text if isinstance(message.text, str) else "")
    if is_new:
        logger.info("Новый пользователь: %s (@%s), источник: %s", user_id, username, source or "—")
        if source:
            try:
                set_user_source(user_id, source)
            except Exception:
                logger.exception("Не удалось сохранить источник для %s", user_id)

    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(Command("help"))
@dp.message(F.text == "📄 Помощь")
async def cmd_help(message: Message):
    """Обработчик команды /help."""
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("ai"))
@dp.message(F.text == "🤖 AI-разбор")
async def cmd_ai(message: Message):
    """
    Команда /ai — принудительный AI-разбор последнего отчёта.
    Можно также использовать кнопку '🤖 AI-разбор' под отчётом.
    """
    user_id = message.from_user.id
    ai = get_ai_client()

    if not ai.available:
        await message.answer(
            AI_UNAVAILABLE_TEXT,
            parse_mode="HTML",
        )
        return

    last = _last_reports.get(user_id)
    if not last:
        await message.answer(
            "Сначала пришли отчёт — потом сможешь получить AI-разбор."
        )
        return

    thinking = await message.answer("🤖 AI анализирует… ⏳")
    try:
        summary = await ai_summarize_report(
            report_text=last["text"],
            marketplace=last["marketplace"],
        )
        await thinking.edit_text(
            f"🤖 <b>AI-разбор твоего отчёта:</b>\n\n{_html_escape(summary)}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("AI-сбой для user=%s: %s", user_id, exc)
        await thinking.edit_text("🤖 Не удалось получить AI-разбор. Попробуй позже.")


@dp.message(Command("buy"))
@dp.message(F.text == "💳 Оплатить доступ")
async def cmd_buy(message: Message):
    """Обработчик команды /buy — показывает варианты оплаты."""
    text = "💎 <b>Тарифы</b>\n\n" + tariffs_text() + "\n\n"
    if ym.enabled():
        text += "Оплата картой или ЮMoney, доступ откроется автоматически в течение минуты:"
    elif PAYMENT_PROVIDER_TOKEN:
        text += "Оплата картой прямо в Telegram, доступ откроется автоматически:"
    else:
        text += (
            "После оплаты пришли сюда чек — доступ откроем вручную.\n"
            "Выбери тариф:"
        )
    text += "\n\n<i>" + legal.links_html() + "</i>"
    await message.answer(
        text, parse_mode="HTML", reply_markup=get_buy_keyboard(),
        link_preview_options=NO_PREVIEW,
    )


# ─── Автооплата (Telegram Payments + ЮKassa) ────────────────────────────────

def _build_invoice_kwargs(plan_key: str, user_id: int) -> dict:
    """Параметры счёта для bot.send_invoice. Вынесено отдельно для тестов."""
    plan = PLANS[plan_key]
    amount_kop = plan["price_rub"] * 100
    kwargs = dict(
        title=plan["title"],
        description=plan["description"],
        payload=f"{plan_key}:{user_id}",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=[LabeledPrice(label=plan["title"], amount=amount_kop)],
        start_parameter=f"buy_{plan_key}",
    )
    if YOOKASSA_SEND_RECEIPT:
        kwargs["need_email"] = True
        kwargs["send_email_to_provider"] = True
        kwargs["provider_data"] = json.dumps({
            "receipt": {
                "items": [{
                    "description": plan["title"][:128],
                    "quantity": "1.00",
                    "amount": {"value": f"{plan['price_rub']:.2f}", "currency": "RUB"},
                    "vat_code": YOOKASSA_VAT_CODE,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }]
            }
        }, ensure_ascii=False)
    return kwargs


async def _plan_still_on_sale(callback: CallbackQuery, plan_key: str) -> bool:
    """Кнопка из старого сообщения могла пережить конец акции — проверяем."""
    if plan_key in available_plans():
        return True
    await callback.message.answer(
        "⏱ Эта акция уже закончилась. Актуальные тарифы:\n\n" + tariffs_text(),
        parse_mode="HTML",
        reply_markup=get_buy_keyboard(),
    )
    return False


def _parse_payload(payload: str) -> tuple[str, int] | None:
    """'month:123' → ('month', 123). None, если payload чужой/битый."""
    try:
        plan_key, uid = payload.split(":", 1)
        if plan_key not in PLANS:
            return None
        return plan_key, int(uid)
    except (ValueError, AttributeError):
        return None


@dp.callback_query(F.data.in_({f"buy_{k}" for k in PLAN_KEYS}))
async def callback_buy(callback: CallbackQuery):
    """Кнопка тарифа → счёт на оплату внутри Telegram."""
    await callback.answer()
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer("Оплата временно недоступна. Напишите /buy позже.")
        return
    plan_key = callback.data.removeprefix("buy_")
    if not await _plan_still_on_sale(callback, plan_key):
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        **_build_invoice_kwargs(plan_key, callback.from_user.id),
    )


@dp.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    """Telegram спрашивает «можно принимать оплату?» — на ответ есть 10 секунд."""
    parsed = _parse_payload(query.invoice_payload)
    if parsed is None:
        await query.answer(ok=False, error_message="Счёт устарел. Нажмите /buy и выберите тариф заново.")
        return
    plan_key, _ = parsed
    if query.total_amount != PLANS[plan_key]["price_rub"] * 100 or query.currency != "RUB":
        await query.answer(ok=False, error_message="Цена изменилась. Нажмите /buy, чтобы получить новый счёт.")
        return
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Деньги получены → продлеваем доступ автоматически."""
    sp = message.successful_payment
    user_id = message.from_user.id
    parsed = _parse_payload(sp.invoice_payload)
    plan_key = parsed[0] if parsed else "month"
    plan = PLANS[plan_key]

    await _grant_paid_access(
        user_id=user_id,
        username=message.from_user.username or "",
        plan_key=plan_key,
        charge_id=sp.telegram_payment_charge_id,
        amount_kop=sp.total_amount,
        provider_id=sp.provider_payment_charge_id or "",
        provider_name="ЮKassa",
    )


async def _grant_paid_access(
    user_id: int,
    username: str,
    plan_key: str,
    charge_id: str,
    amount_kop: int,
    provider_id: str,
    provider_name: str,
) -> bool:
    """
    Общая выдача доступа после оплаты (ЮKassa или ЮMoney).
    Возвращает False, если этот платёж уже был засчитан раньше.
    """
    plan = PLANS[plan_key]
    add_user(user_id, username)
    is_new = record_payment(
        charge_id=charge_id,
        user_id=user_id,
        plan=plan_key,
        amount=amount_kop,
        provider_charge_id=provider_id,
    )
    if not is_new:
        logger.warning("Повторное уведомление об оплате %s — пропускаю", charge_id)
        return False

    until = extend_access(user_id, plan["days"])
    until_text = "навсегда" if plan_key == "forever" else f"до {until[:10]}"
    receipt_no, receipt_link = await _issue_receipt(charge_id, user_id, plan_key, amount_kop)
    try:
        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата прошла!</b>\n\nДоступ открыт {until_text}.\n"
            "Присылай отчёт — разберу его за секунды.\n\n"
            + ("🧾 Чек — в следующем сообщении." if receipt_link else
               "🧾 Чек из «Мой налог» пришлю сюда в течение дня. Все чеки — /receipts."),
            parse_mode="HTML",
            reply_markup=get_main_keyboard(),
        )
        if receipt_link:
            await _send_receipt_to_user(user_id, receipt_link)
    except Exception:
        logger.exception("Не удалось сообщить пользователю %s об оплате", user_id)
    logger.info("Оплата (%s): user=%s plan=%s amount=%s", provider_name, user_id, plan_key, amount_kop)

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Новая оплата ({provider_name})</b>\n\n"
                f"@{_html_escape(username or '—')} (<code>{user_id}</code>)\n"
                f"Тариф: {plan['title']}\n"
                f"Сумма: {amount_kop / 100:.0f} руб.\n"
                f"ID платежа: <code>{_html_escape(provider_id or charge_id)}</code>\n"
                + (f"🧾 Чек №{receipt_no} создан автоматически ✅" if receipt_link
                   else f"🧾 Чек №{receipt_no}: нужно создать вручную ⬇️"),
                parse_mode="HTML",
            )
            if not receipt_link:
                await bot.send_message(
                    ADMIN_ID,
                    NR.manual_instructions(
                        receipt_no, plan_key, amount_kop / 100,
                        datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
                    ),
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Не удалось уведомить админа об оплате")
    return True


# ─── Чеки самозанятого («Мой налог») ────────────────────────────────────────

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def _issue_receipt(charge_id: str, user_id: int, plan_key: str, amount_kop: int) -> tuple[int, str]:
    """Заводит чек для платежа и пробует создать его автоматически. → (номер, ссылка|'')."""
    try:
        receipt_no = add_receipt(charge_id, user_id, plan_key, amount_kop)
    except Exception:
        logger.exception("Чек: не удалось сохранить запись для %s", charge_id)
        return 0, ""
    client = NR.get_client()
    if client is None:
        return receipt_no, ""
    try:
        _uuid, link = await client.create_receipt(plan_key, amount_kop / 100)
        set_receipt_url(receipt_no, link)
        logger.info("Чек №%s создан в «Мой налог»: %s", receipt_no, link)
        return receipt_no, link
    except Exception as exc:
        logger.warning("Чек №%s: авто-создание не удалось, нужен ручной: %s", receipt_no, exc)
        return receipt_no, ""


def _receipt_keyboard(link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧾 Открыть чек", url=link),
    ]])


async def _send_receipt_to_user(user_id: int, link: str) -> None:
    await bot.send_message(
        user_id,
        "🧾 <b>Чек об оплате</b> (сформирован в «Мой налог», ФНС).\n"
        "Все чеки — по команде /receipts.",
        parse_mode="HTML",
        reply_markup=_receipt_keyboard(link),
    )


def _receipt_line(r: dict) -> str:
    plan = PLANS.get(r["plan"], {}).get("title", r["plan"])
    date_s = (r.get("created_at") or "")[:10]
    head = f"№{r['id']} · {date_s} · {plan} · {r['amount'] / 100:.0f} ₽"
    if r.get("url"):
        return f'{head}\n   <a href="{_html_escape(r["url"])}">🧾 открыть чек</a>'
    return f"{head}\n   ⏳ чек формируется"


@dp.message(Command("receipts"))
async def cmd_receipts(message: Message):
    """Чеки пользователя (и очередь ручных чеков — для админа)."""
    user_id = message.from_user.id
    rows = get_user_receipts(user_id)
    if rows:
        text = "🧾 <b>Твои чеки</b>\n\n" + "\n\n".join(_receipt_line(r) for r in rows)
    else:
        text = "🧾 Оплат пока не было — чеков нет.\nТарифы: /buy"
    if user_id == ADMIN_ID:
        pending = get_pending_receipts()
        if pending:
            text += "\n\n🛠 <b>Ждут ручного чека:</b>\n" + "\n".join(
                f"№{r['id']} · user <code>{r['user_id']}</code> · {r['amount'] / 100:.2f} ₽ · {r['plan']}"
                for r in pending
            ) + "\n\nДетали чека: /receipt НОМЕР"
    await message.answer(text, parse_mode="HTML", link_preview_options=NO_PREVIEW)


@dp.message(Command("receipt"))
async def cmd_receipt(message: Message):
    """Админ: /receipt 7 https://lknpd.nalog.ru/... — отдать покупателю ссылку на чек."""
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/receipt НОМЕР ССЫЛКА</code>\nОчередь: /receipts", parse_mode="HTML")
        return
    receipt_no = int(parts[1])
    if len(parts) == 2:
        row = next((r for r in get_pending_receipts() if r["id"] == receipt_no), None)
        if not row:
            await message.answer(f"Чек №{receipt_no} не найден среди ожидающих.")
            return
        await message.answer(
            NR.manual_instructions(receipt_no, row["plan"], row["amount"] / 100, row["created_at"][:16].replace("T", " ") + " UTC"),
            parse_mode="HTML",
        )
        return
    link = parts[2].strip()
    if not NR.looks_like_receipt_url(link):
        await message.answer("Это не похоже на ссылку на чек «Мой налог» (https://lknpd.nalog.ru/…).")
        return
    row = set_receipt_url(receipt_no, link)
    if not row:
        await message.answer(f"Чек №{receipt_no} не найден.")
        return
    try:
        await _send_receipt_to_user(row["user_id"], link)
        await message.answer(f"✅ Чек №{receipt_no} отправлен покупателю.")
    except Exception:
        logger.exception("Не удалось отправить чек пользователю %s", row["user_id"])
        await message.answer(f"Чек №{receipt_no} сохранён, но отправить покупателю не удалось (он мог заблокировать бота).")


# ─── Документы: оферта, политика, удаление данных ───────────────────────────

async def _send_legal(message: Message, kind: str) -> None:
    link = legal.url(kind)
    if link:
        await message.answer(
            f"📄 <b>{_html_escape(legal.title(kind))}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Открыть документ", url=link),
            ]]),
        )
        return
    for chunk in legal.to_telegram_chunks(legal.render(kind)):
        await message.answer(chunk, parse_mode="HTML")


@dp.message(Command("terms"))
async def cmd_terms(message: Message):
    await _send_legal(message, "offer")


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message):
    await _send_legal(message, "privacy")


@dp.message(Command("delete_me"))
async def cmd_delete_me(message: Message):
    await message.answer(
        "🗑 <b>Удалить мои данные?</b>\n\n"
        "Удалятся: себестоимость, налоговые настройки, показатели последнего отчёта, "
        "отзывы и имя пользователя.\n"
        "Останутся: сведения об оплатах и чеках (их нужно хранить по налоговому "
        "законодательству) и срок доступа.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, удалить", callback_data="delete_me_yes"),
            InlineKeyboardButton(text="Отмена", callback_data="delete_me_no"),
        ]]),
    )


@dp.callback_query(F.data.in_({"delete_me_yes", "delete_me_no"}))
async def callback_delete_me(callback: CallbackQuery):
    await callback.answer()
    if callback.data == "delete_me_no":
        await callback.message.edit_text("Ок, ничего не удаляю.")
        return
    user_id = callback.from_user.id
    delete_user_data(user_id)
    _cost_state.pop(user_id, None)
    _compare_state.pop(user_id, None)
    logger.info("Пользователь %s удалил свои данные (/delete_me)", user_id)
    await callback.message.edit_text("✅ Данные удалены.")


# ─── Автооплата через кошелёк ЮMoney ────────────────────────────────────────

def _ym_pay_keyboard(url: str, amount_rub: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount_rub} руб.", url=url)],
        [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data="ym_check")],
    ])


@dp.callback_query(F.data.in_({f"ym_{k}" for k in PLAN_KEYS}))
async def callback_ym_buy(callback: CallbackQuery):
    """Тариф → уникальная ссылка на оплату ЮMoney с меткой пользователя."""
    await callback.answer()
    user_id = callback.from_user.id
    plan_key = callback.data.removeprefix("ym_")
    if not await _plan_still_on_sale(callback, plan_key):
        return
    plan = PLANS[plan_key]
    add_user(user_id, callback.from_user.username or "")

    label = ym.make_label(plan_key, user_id)
    try:
        url = await ym.create_payment_url(label, plan["price_rub"], plan["title"])
    except Exception:
        logger.exception("ЮMoney: не удалось создать ссылку для user=%s", user_id)
        await callback.message.answer("😔 Не получилось создать ссылку на оплату. Попробуй через минуту: /buy")
        return
    add_pending_payment(label, user_id, plan_key)
    await callback.message.answer(
        f"🧾 <b>{plan['title']}</b> — {plan['price_rub']} руб.\n\n"
        "1️⃣ Нажми «Оплатить» и заплати картой или кошельком ЮMoney.\n"
        "2️⃣ Доступ откроется автоматически в течение минуты.\n\n"
        "Если ничего не пришло — нажми «Я оплатил».\n"
        "<i>Ссылка действует 48 часов.</i>\n\n"
        f"<i>{legal.links_html()}</i>",
        parse_mode="HTML",
        reply_markup=_ym_pay_keyboard(url, plan["price_rub"]),
        link_preview_options=NO_PREVIEW,
    )


async def _check_yoomoney(user_id: int | None = None) -> int:
    """Ищет оплаты по ожидающим меткам. Возвращает количество засчитанных платежей."""
    pending = get_pending_payments(user_id=user_id)
    if not pending:
        return 0
    operations = await ym.fetch_incoming(ym.since_for(pending))
    prices = {k: v["price_rub"] for k, v in PLANS.items()}
    granted = 0
    for f in ym.match_payments(operations, pending, prices):
        ok = await _grant_paid_access(
            user_id=f["user_id"],
            username="",
            plan_key=f["plan"],
            charge_id=f"yoomoney:{f['operation_id']}",
            amount_kop=int(round(f["amount"] * 100)),
            provider_id=f["operation_id"],
            provider_name="ЮMoney",
        )
        mark_pending_paid(f["label"])
        granted += int(ok)
    return granted


@dp.callback_query(F.data == "ym_check")
async def callback_ym_check(callback: CallbackQuery):
    """Кнопка «Я оплатил» — проверяем сразу, не дожидаясь фоновой проверки."""
    await callback.answer("Проверяю…")
    try:
        granted = await _check_yoomoney(callback.from_user.id)
    except Exception:
        logger.exception("ЮMoney: ошибка проверки для user=%s", callback.from_user.id)
        await callback.message.answer("😔 Не удалось проверить оплату. Попробуй через минуту.")
        return
    if granted:
        return  # сообщение «Оплата прошла» уже отправлено
    if not get_pending_payments(user_id=callback.from_user.id):
        await callback.message.answer("✅ Этот платёж уже засчитан. Проверь: «📈 Моя статистика».")
        return
    await callback.message.answer(
        "⏳ Платёж пока не найден. Обычно он появляется в течение 1–2 минут после оплаты — "
        "бот проверит сам и пришлёт сообщение.\n\n"
        "Если прошло больше 10 минут — нажми /paysupport и приложи чек."
    )


async def _yoomoney_poller(interval: int = 30):
    """Фоновая проверка оплат ЮMoney каждые interval секунд."""
    logger.info("ЮMoney: фоновая проверка оплат включена (каждые %s с)", interval)
    while True:
        try:
            await _check_yoomoney()
        except Exception as exc:
            logger.warning("ЮMoney: проверка не удалась: %s", exc)
        await asyncio.sleep(interval)


@dp.message(Command("paysupport"))
async def cmd_paysupport(message: Message):
    """Telegram требует от ботов с платежами команду поддержки по оплатам."""
    _pending_problem[message.from_user.id] = "вопрос по оплате"
    await message.answer(
        "💬 <b>Вопрос по оплате</b>\n\n"
        "Опишите проблему одним сообщением (можно приложить скриншот чека) — "
        "его получит администратор.\n\n"
        "Отправьте /cancel, чтобы выйти.",
        parse_mode="HTML",
    )


# ─── Пример отчёта, статистика, приглашение ─────────────────────────────────

@dp.message(Command("demo"))
@dp.message(F.text == "📊 Получить пример отчёта")
async def cmd_demo(message: Message):
    await message.answer(
        "📊 <b>Какой пример прислать?</b>\n\n"
        "Это реалистичный отчёт магазина. Скачай его и отправь мне обратно — "
        "увидишь полный разбор: прибыль, штрафы, ABC-анализ и советы.",
        parse_mode="HTML",
        reply_markup=get_demo_keyboard(),
    )


@dp.callback_query(F.data.startswith("demo_"))
async def callback_demo(callback: CallbackQuery):
    await callback.answer()
    marketplace = callback.data.removeprefix("demo_")
    path = DEMO_FILES.get(marketplace)
    if not path or not os.path.exists(path):
        await callback.message.answer("😔 Пример сейчас недоступен. Попробуй позже.")
        logger.error("Демо-файл не найден: %s", path)
        return
    how = (
        "WB: Финансы → Детализация" if marketplace == "WB"
        else "Ozon: Финансы → Детализация начислений"
    )
    await callback.message.answer_document(
        FSInputFile(path, filename=f"пример_отчёта_{marketplace}.xlsx"),
        caption=(
            f"📎 Пример отчёта {marketplace}.\n\n"
            "👉 Перешли этот файл обратно в чат — получишь разбор.\n"
            f"Свой отчёт выгружается здесь: {how}."
        ),
    )


@dp.message(Command("stats"))
@dp.message(F.text == "📈 Моя статистика")
async def cmd_stats(message: Message):
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username or "")
    u = get_user(user_id) or {}
    active = is_trial_active(user_id)
    if u.get("access_until"):
        status = f"💎 Оплачен до {u['access_until'][:10]}" if active else "⏱ Оплаченный доступ закончился"
    else:
        from datetime import datetime, timedelta
        from database import FREE_TRIAL_DAYS
        try:
            end = datetime.fromisoformat(u["join_date"]) + timedelta(days=FREE_TRIAL_DAYS)
            status = f"🆓 Пробный период до {end:%Y-%m-%d}" if active else "⏱ Пробный период закончился"
        except (KeyError, TypeError, ValueError):
            status = "🆓 Пробный период"
    text = (
        "📈 <b>Твоя статистика</b>\n\n"
        f"{status}\n"
        f"📁 Проанализировано отчётов: <b>{u.get('report_count', 0)}</b>"
    )
    if not active:
        text += "\n\nПродлить доступ → /buy"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("invite"))
@dp.message(F.text == "🎁 Пригласить друга")
async def cmd_invite(message: Message):
    me = await _get_me()
    name = me.username if me and getattr(me, "username", None) else "wildfinance_bot"
    link = f"https://t.me/{name}?start=ref_{message.from_user.id}"
    await message.answer(
        "🎁 Поделись ботом с другом-селлером — у него будет 7 дней бесплатно:\n\n"
        f"{link}",
    )


_me_cache = None


async def _get_me():
    global _me_cache
    if _me_cache is None:
        try:
            _me_cache = await bot.get_me()
        except Exception:
            return None
    return _me_cache


@dp.message(Command("give_access"))
async def cmd_give_access(message: Message):
    """
    Админ-команда: /give_access <user_id> <дней>
    Работает только для ADMIN_ID.
    """
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer(
            "Формат: /give_access &lt;user_id&gt; &lt;дней&gt;\n"
            "Пример: /give_access 123456789 30",
            parse_mode="HTML",
        )
        return

    try:
        target_user_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.answer("❌ user_id и дней должны быть числами.")
        return

    if days <= 0 or days > 3650:
        await message.answer("❌ Количество дней: от 1 до 3650.")
        return

    success = grant_access(target_user_id, days)
    if success:
        await message.answer(
            f"✅ Доступ пользователю <code>{target_user_id}</code> "
            f"выдан на <b>{days} дн.</b>",
            parse_mode="HTML",
        )
        # Уведомляем пользователя
        try:
            await bot.send_message(
                target_user_id,
                f"🎉 Вам выдан доступ на <b>{days} дн.</b>\n"
                f"Отправьте отчёт для анализа!",
                parse_mode="HTML",
            )
        except Exception:
            pass
        logger.info("Админ выдал доступ: user=%s, days=%s", target_user_id, days)
    else:
        await message.answer(
            f"❌ Пользователь <code>{target_user_id}</code> не найден в БД.\n"
            f"Он должен сначала написать боту /start.",
            parse_mode="HTML",
        )


@dp.message(Command("sources"))
async def cmd_sources(message: Message):
    """Админ: откуда приходят пользователи и кто из них платит (ссылки t.me/бот?start=метка)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return
    stats = get_source_stats()
    if not stats:
        await message.answer("Пока нет пользователей.")
        return
    lines = ["📈 <b>Источники пользователей</b>", ""]
    for st in stats:
        conv = f"{st['payers'] / st['users'] * 100:.0f}%" if st["users"] else "—"
        lines.append(
            f"<b>{_html_escape(st['source'])}</b>: {st['users']} чел. · "
            f"прислали отчёт {st['active']} · оплатили {st['payers']} ({conv}) · "
            f"{st['revenue_kop'] / 100:,.0f} ₽"
        )
    lines += ["", "Ссылка с меткой: <code>https://t.me/wildfinance_bot?start=wbchat</code>"]
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("users"))
async def cmd_users(message: Message):
    """
    Админ-команда: /users
    Показывает статистику пользователей.
    Работает только для ADMIN_ID.
    """
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет доступа к этой команде.")
        return

    users = get_all_users_stats()
    if not users:
        await message.answer("В базе пока нет пользователей.")
        return

    pay_count, pay_sum = get_payments_total()
    text = (
        f"📊 <b>Пользователи ({len(users)} чел.)</b>\n"
        f"💰 Автооплат: {pay_count} на {pay_sum / 100:,.0f} руб.\n\n"
    )
    for u in users:
        uname = f"@{_html_escape(u['username'])}" if u['username'] else "без_юзернейма"
        text += f"👤 <code>{u['user_id']}</code> | {uname}\n"
        text += f"   📅 Рег: {u['join_date'][:10]}\n"
        text += f"   📁 Отчётов: {u['report_count']}\n"
        
        if u['access_until']:
            text += f"   💎 До: {u['access_until'][:10]}\n"
        else:
            text += f"   🆓 Триал\n"
        text += "\n"

    # Если текст слишком длинный, разобьем его
    if len(text) <= 4000:
        await message.answer(text, parse_mode="HTML")
    else:
        chunks = split_text(text, max_len=4000)
        for chunk in chunks:
            await message.answer(chunk, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
# ХЕНДЛЕР ДОКУМЕНТОВ
# ═══════════════════════════════════════════════════════════════════════════════

# Словарь для хранения состояния ожидания отзыва {user_id: stars}
_pending_feedback: dict[int, int] = {}

# Пользователи, нажавшие «Сообщить о проблеме»: {user_id: имя_последнего_файла}.
# Ставится ТОЛЬКО по кнопке — иначе любое следующее сообщение улетало бы админу как жалоба.
_pending_problem: dict[int, str] = {}

# Имя последнего проанализированного файла {user_id: file_name} — для жалоб
_last_file_name: dict[int, str] = {}

# Состояние /compare: {user_id: {"old": metrics | None, "old_name": str}}
_compare_state: dict[int, dict] = {}

# Последнее сравнение (текстовая сводка для AI-кнопки) {user_id: {"plain": str, "marketplace": str}}
_last_compare: dict[int, dict] = {}

# Словарь последних отчётов {user_id: {"text": str, "marketplace": str, ...}}
# Нужен для AI-кнопки и команды /compare
_last_reports: dict[int, dict] = {}


@dp.message(F.document)
async def handle_document(message: Message):
    """Обработчик входящих файлов (.xlsx, .xls, .csv)."""
    user_id = message.from_user.id
    username = message.from_user.username or ""

    # Регистрируем пользователя, если ещё не в БД
    add_user(user_id, username)

    # Проверяем доступ (триал или оплаченный)
    if not is_trial_active(user_id):
        await message.answer(trial_expired_text(), parse_mode="HTML", reply_markup=get_buy_keyboard())
        return

    # Файл с себестоимостью (наш шаблон) — сохраняем цены, а не анализируем как отчёт
    if (_cost_state.get(user_id, {}).get("mode") == "file"
            or "себестоим" in (message.document.file_name or "").lower()):
        await _handle_cost_file(message)
        return

    # Режим /compare — файл идёт в сравнение, а не в обычный анализ
    if user_id in _compare_state:
        await _handle_compare_file(message)
        return

    # Проверяем расширение файла
    file_name = message.document.file_name or ""
    ext = os.path.splitext(file_name)[1].lower()

    if ext not in SUPPORTED_EXTENSIONS:
        await message.answer(
            "❌ Неподдерживаемый формат файла.\n"
            "Отправь файл в формате .xlsx, .xls или .csv"
        )
        return

    # Размер файла
    file_size_mb = (message.document.file_size or 0) / (1024 * 1024)
    if file_size_mb > 18:
        await message.answer(
            "📦 Файл слишком большой (больше 18 МБ).\n\n"
            "Попробуй разбить отчёт на несколько частей."
        )
        return

    tmp_path = None
    processing_msg = None

    try:
        processing_msg = await message.answer(
            "⏳ Анализирую отчёт...\n"
            "Обычно это занимает 5–15 секунд."
        )

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=ext, prefix="wb_report_"
        ) as tmp:
            tmp_path = tmp.name

        await bot.download(message.document, destination=tmp_path)
        logger.info("Файл скачан: %s -> %s (%.2f МБ)", file_name, tmp_path, file_size_mb)

        # Анализируем в отдельном потоке, чтобы не блокировать event loop
        result, metrics = await asyncio.to_thread(analyze_full, tmp_path)
        marketplace = metrics.get("marketplace", "?")
        logger.info("Маркетплейс=%s, файл='%s', user=%s", marketplace, file_name, user_id)

        # Снимок по артикулам — для ввода себестоимости (живёт в БД, переживает перезапуск)
        snapshot = None
        try:
            snapshot = P.snapshot_from_metrics(metrics)
            save_last_report(user_id, snapshot, file_name)
        except Exception:
            logger.exception("Не удалось сохранить снимок отчёта для %s", user_id)

        # Удаляем «анализирую...»
        try:
            await processing_msg.delete()
        except Exception:
            pass

        # Отправляем результат (HTML). Кнопки действий — прямо под последней частью
        # отчёта (без отдельного сообщения «Что дальше?»), реклама тарифов — только
        # тем, у кого нет оплаченного доступа.
        shown = result + report_promo_footer(user_id)
        chunks = split_text(shown, max_len=4000)
        for i, chunk in enumerate(chunks):
            await message.answer(
                chunk,
                parse_mode="HTML",
                reply_markup=get_post_report_keyboard() if i == len(chunks) - 1 else None,
            )

        # Если себестоимость уже известна — сразу показываем настоящую прибыль
        if snapshot and get_costs(user_id, snapshot["marketplace"]):
            await _send_profit(message, user_id)

        # Запоминаем имя файла, чтобы привязать возможную жалобу к отчёту
        _last_file_name[user_id] = file_name

        # Сохраняем результат последнего отчёта (для AI-кнопки и сравнений)
        _last_reports[user_id] = {
            "text": result,
            "marketplace": marketplace if marketplace != "?" else "WB",
            "file_name": file_name,
            "timestamp": __import__("time").time(),
        }

        # Увеличиваем счётчик отчётов
        report_num = increment_report_count(user_id)
        logger.info("Отчёт #%s отправлен пользователю %s", report_num, user_id)

        # После первого отчёта — запрос обратной связи
        if report_num == 1 and not has_given_feedback(user_id):
            await asyncio.sleep(1)  # Пауза чтобы пользователь увидел отчёт
            await message.answer(
                "🙏 <b>Это был ваш первый отчёт!</b>\n\n"
                "Помогите мне стать лучше — оцените результат.\n"
                "Сколько звёзд вы поставите?",
                parse_mode="HTML",
                reply_markup=get_feedback_keyboard(),
            )

    except asyncio.TimeoutError:
        logger.warning("Timeout для пользователя %s, файл: %s", user_id, file_name)
        if processing_msg:
            try:
                await processing_msg.delete()
            except Exception:
                pass
        await message.answer(ERROR_TIMEOUT)

    except Exception as exc:
        logger.error(
            "Ошибка при обработке файла '%s' от пользователя %s:\n%s",
            file_name, user_id, traceback.format_exc(),
        )
        if processing_msg:
            try:
                await processing_msg.delete()
            except Exception:
                pass
        user_msg = classify_error(exc)
        await message.answer(user_msg)

        # ── Пересылаем админу файл + traceback + результат (если был) ─────
        if ADMIN_ID:
            try:
                username = message.from_user.username or "—"
                admin_text = (
                    f"🚨 <b>Ошибка при анализе отчёта</b>\n\n"
                    f"Пользователь: @{_html_escape(username)} (<code>{user_id}</code>)\n"
                    f"Файл: <code>{_html_escape(file_name)}</code>\n"
                    f"Тип исключения: <code>{type(exc).__name__}</code>\n"
                    f"Сообщение: {_html_escape(str(exc)[:300])}\n\n"
                )
                # Если что-то проанализировалось — добавим в уведомление
                if "result" in locals() and result:
                    snippet = result[:1500]
                    admin_text += (
                        f"📋 <b>Частичный результат анализа (первые 1500 символов):</b>\n"
                        f"<pre>{_html_escape(snippet)}</pre>\n\n"
                    )
                else:
                    admin_text += "📋 Результат анализа: <i>не получен (упало до формирования отчёта)</i>\n\n"

                admin_text += f"🔍 <b>Traceback:</b>\n<pre>{_html_escape(traceback.format_exc())[:3000]}</pre>"

                await bot.send_message(
                    ADMIN_ID,
                    admin_text,
                    parse_mode="HTML",
                )
                # Пересылаем сам файл как документ (если остался на диске)
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        await bot.send_document(
                            ADMIN_ID,
                            FSInputFile(tmp_path, filename=file_name or "report"),
                            caption=(
                                f"📎 Копия файла от @{username} "
                                f"(<code>{user_id}</code>)"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as send_err:
                        logger.warning("Не удалось переслать файл админу: %s", send_err)
                logger.info("Админ уведомлён об ошибке для user=%s", user_id)
            except Exception as notify_err:
                logger.error("Не удалось уведомить админа: %s", notify_err)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.info("Временный файл удалён: %s", tmp_path)
            except OSError as e:
                logger.warning("Не удалось удалить файл %s: %s", tmp_path, e)


# ═══════════════════════════════════════════════════════════════════════════════
# /compare — СРАВНЕНИЕ ДВУХ ОТЧЁТОВ
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message(Command("compare"))
async def cmd_compare(message: Message):
    """Запускает режим сравнения: ждём два файла (старый, затем новый)."""
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username or "")
    if not is_trial_active(user_id):
        await message.answer(trial_expired_text(), parse_mode="HTML", reply_markup=get_buy_keyboard())
        return
    _compare_state[user_id] = {"old": None, "old_name": ""}
    await message.answer(
        "📊 <b>Сравнение отчётов</b>\n\n"
        "1️⃣ Пришли <b>старый</b> отчёт (например, за прошлую неделю)\n"
        "2️⃣ Затем — <b>новый</b>\n\n"
        "Покажу, что выросло и что просело: прибыль, расходы и каждый артикул.\n"
        "Отчёты должны быть одного маркетплейса.\n\n"
        "/cancel — выйти из режима сравнения",
        parse_mode="HTML",
    )


@dp.message(Command("wbozon"))
@dp.message(F.text == "⚖️ WB против Ozon")
async def cmd_wbozon(message: Message):
    """Сравнение площадок: где с рубля выручки остаётся больше."""
    user_id = message.from_user.id
    add_user(user_id, message.from_user.username or "")
    if not is_trial_active(user_id):
        await message.answer(trial_expired_text(), parse_mode="HTML", reply_markup=get_buy_keyboard())
        return
    _compare_state[user_id] = {"cross": True, "reports": {}, "old": None, "old_name": ""}
    await message.answer(
        "⚖️ <b>WB против Ozon</b>\n\n"
        "Пришли два отчёта за <b>одинаковый период</b>, в любом порядке:\n"
        "• WB — детализация еженедельного отчёта\n"
        "• Ozon — отчёт по начислениям\n\n"
        "Покажу, сколько каждая площадка забирает с 100 ₽ выручки, где выгоднее продавать "
        "одинаковые товары и в какой статье расходов главная разница.\n\n"
        "/cancel — выйти",
        parse_mode="HTML",
    )


async def _compute_uploaded(message: Message) -> dict:
    """Скачивает присланный файл во временный, считает метрики, удаляет файл."""
    file_name = message.document.file_name or ""
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError("Неподдерживаемый формат файла. Нужен .xlsx, .xls или .csv")
    if (message.document.file_size or 0) > 18 * 1024 * 1024:
        raise ValueError("Файл больше 18 МБ")
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="cmp_") as tmp:
        tmp_path = tmp.name
    try:
        await bot.download(message.document, destination=tmp_path)
        return await asyncio.to_thread(compute, tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


async def _handle_compare_file(message: Message):
    """Обрабатывает файл, присланный в режиме /compare."""
    user_id = message.from_user.id
    state = _compare_state.get(user_id) or {"old": None, "old_name": ""}
    file_name = message.document.file_name or "файл"

    try:
        metrics = await _compute_uploaded(message)
    except Exception as exc:
        logger.warning("compare: не удалось прочитать '%s' от %s: %s", file_name, user_id, exc)
        await message.answer(
            classify_error(exc) + "\n\n(Режим сравнения активен — пришли файл ещё раз или /cancel)"
        )
        return

    # Режим «WB против Ozon»: ждём по одному отчёту каждой площадки, порядок любой
    if state.get("cross"):
        got = state.setdefault("reports", {})
        got[metrics["marketplace"]] = metrics
        need = {"WB", "Ozon"} - set(got)
        if need:
            _compare_state[user_id] = state
            await message.answer(
                f"✅ Отчёт {metrics['marketplace']} принят. Теперь пришли отчёт <b>{need.pop()}</b>.",
                parse_mode="HTML",
            )
            return
        _compare_state.pop(user_id, None)
        await message.answer(compare_marketplaces(got["WB"], got["Ozon"]), parse_mode="HTML")
        return

    if state["old"] is None:
        state["old"], state["old_name"] = metrics, file_name
        _compare_state[user_id] = state
        await message.answer(
            f"✅ Старый отчёт принят ({metrics['marketplace']}): <code>{_html_escape(file_name)}</code>\n\n"
            "2️⃣ Теперь пришли <b>новый</b> отчёт.",
            parse_mode="HTML",
        )
        return

    old = state["old"]
    if old["marketplace"] != metrics["marketplace"]:
        # Прислали WB и Ozon — это не «было/стало», а сравнение площадок
        _compare_state.pop(user_id, None)
        await message.answer(
            "ℹ️ Это отчёты разных площадок — показываю сравнение WB и Ozon.\n"
            "(Для «было → стало» нужны два отчёта одной площадки.)"
        )
        await message.answer(compare_marketplaces(old, metrics), parse_mode="HTML")
        return
    text = compare_metrics(old, metrics)

    _compare_state.pop(user_id, None)
    _last_compare[user_id] = {
        "plain": compare_plain(old, metrics),
        "marketplace": metrics["marketplace"],
    }
    header = (
        f"<i>{_html_escape(state['old_name'])} → {_html_escape(file_name)}</i>\n\n"
    )
    ai_markup = get_compare_ai_keyboard() if get_ai_client().available else None
    chunks = split_text(header + text, max_len=4000)
    for i, chunk in enumerate(chunks):
        await message.answer(
            chunk,
            parse_mode="HTML",
            reply_markup=ai_markup if i == len(chunks) - 1 else None,
        )
    logger.info("compare: user=%s %s", user_id, metrics["marketplace"])


@dp.callback_query(F.data == "cmp_ai")
async def callback_compare_ai(callback: CallbackQuery):
    """AI-выводы по последнему сравнению (LLM получает уже посчитанные цифры)."""
    await callback.answer()
    user_id = callback.from_user.id
    last = _last_compare.get(user_id)
    if not last:
        await callback.message.answer("Сначала сравни два отчёта: /compare")
        return
    if not get_ai_client().available:
        await callback.message.answer(AI_UNAVAILABLE_TEXT, parse_mode="HTML")
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    thinking = await callback.message.answer("🤖 AI сравнивает… ⏳")
    try:
        summary = await ai_compare_reports(
            old_report="(см. сводку изменений в новом отчёте)",
            new_report=last["plain"],
            marketplace=last["marketplace"],
        )
        await thinking.edit_text(
            f"🤖 <b>AI-выводы по сравнению:</b>\n\n{_html_escape(summary)}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("AI compare сбой user=%s: %s", user_id, exc)
        await thinking.edit_text("🤖 Не удалось получить AI-выводы. Попробуй позже.")


# ═══════════════════════════════════════════════════════════════════════════════
# СЕБЕСТОИМОСТЬ И НАЛОГИ → НАСТОЯЩАЯ ПРИБЫЛЬ
# ═══════════════════════════════════════════════════════════════════════════════

# Что сейчас вводит пользователь:
#   {"mode": "one_value", "idx": i} — цену одного артикула
#   {"mode": "manual_article"}      — артикул вручную
#   {"mode": "list", "order": [...]} — цены списком
#   {"mode": "file"}                 — ждём заполненный Excel-шаблон
#   {"mode": "tax_custom"}           — свою ставку налога
_cost_state: dict[int, dict] = {}

COST_PAGE_SIZE = 8
COST_CHAT_LIMIT = 40
COST_TEMPLATE_COL = "Себестоимость 1 шт, ₽"


def _tax_for(user_id: int) -> "P.TaxSettings":
    mode, rate = get_tax(user_id)
    return P.TaxSettings(mode=mode, rate=rate)


def _profit_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🧾 Налог: {_tax_for(user_id).label}", callback_data="tax_menu")],
        [InlineKeyboardButton(text="💰 Изменить себестоимость", callback_data="cost_menu")],
    ])


def _money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ") + " ₽"


async def _send_profit(message: Message, user_id: int) -> None:
    snap = get_last_report(user_id)
    if not snap:
        await message.answer("Сначала пришли отчёт WB или Ozon — посчитаю прибыль по нему.")
        return
    tax = _tax_for(user_id)
    p = P.build_profit(snap, get_costs(user_id, snap["marketplace"]), tax)
    await message.answer(P.render_profit_block(p, tax), parse_mode="HTML",
                         reply_markup=_profit_keyboard(user_id))


def _cost_items(user_id: int) -> tuple[dict | None, list[dict]]:
    snap = get_last_report(user_id)
    return snap, (snap["items"] if snap else [])


@dp.callback_query(F.data == "cost_menu")
async def callback_cost_menu(callback: CallbackQuery):
    await callback.answer()
    snap, items = _cost_items(callback.from_user.id)
    if not items:
        await callback.message.answer("Сначала пришли отчёт WB или Ozon — в нём я возьму список артикулов.")
        return
    saved = get_costs(callback.from_user.id, snap["marketplace"])
    known = sum(1 for it in items if it["article"] in saved)
    await callback.message.answer(
        "💰 <b>Себестоимость → настоящая прибыль</b>\n\n"
        "Себестоимость 1 шт = закупка + доставка до склада + упаковка и маркировка.\n"
        f"В отчёте {len(items)} арт., себестоимость известна для {known}.\n\n"
        "Для каких товаров посчитать прибыль?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Один артикул", callback_data="cost_one"),
             InlineKeyboardButton(text="📋 Все артикулы", callback_data="cost_all")],
        ]),
    )


def _cost_page_keyboard(items: list[dict], page: int) -> InlineKeyboardMarkup:
    order = P.interesting_order(items)
    chunk = order[page * COST_PAGE_SIZE:(page + 1) * COST_PAGE_SIZE]
    rows = []
    for i in chunk:
        it = items[i]
        icon = "🔻" if it["payout"] < 0 else "🔹"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {it['article'][:28]} · {_money(it['payout'])}",
            callback_data=f"cost_pick:{i}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cost_page:{page - 1}"))
    if (page + 1) * COST_PAGE_SIZE < len(order):
        nav.append(InlineKeyboardButton(text="➡️ Ещё", callback_data=f"cost_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="⌨️ Ввести артикул вручную", callback_data="cost_manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "cost_one")
async def callback_cost_one(callback: CallbackQuery):
    await callback.answer()
    _, items = _cost_items(callback.from_user.id)
    if not items:
        await callback.message.answer("Сначала пришли отчёт.")
        return
    await callback.message.answer(
        "Выбери товар (сначала — убыточные и самые крупные):",
        reply_markup=_cost_page_keyboard(items, 0),
    )


@dp.callback_query(F.data.startswith("cost_page:"))
async def callback_cost_page(callback: CallbackQuery):
    await callback.answer()
    _, items = _cost_items(callback.from_user.id)
    try:
        page = int(callback.data.split(":", 1)[1])
        await callback.message.edit_reply_markup(reply_markup=_cost_page_keyboard(items, page))
    except Exception:
        pass


async def _ask_cost_for(message: Message, user_id: int, idx: int) -> None:
    snap, items = _cost_items(user_id)
    it = items[idx]
    current = get_costs(user_id, snap["marketplace"]).get(it["article"])
    _cost_state[user_id] = {"mode": "one_value", "idx": idx}
    name = f" — {_html_escape(it['name'])}" if it.get("name") else ""
    now = f"\nСейчас указано: <b>{_money(current)}</b>" if current else ""
    await message.answer(
        f"🔹 <b>{_html_escape(it['article'])}</b>{name}\n"
        f"Продано: {it['units']:g} шт · к выплате {_money(it['payout'])}{now}\n\n"
        "Напиши себестоимость <b>1 штуки</b> в рублях, например <code>850</code>.\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("cost_pick:"))
async def callback_cost_pick(callback: CallbackQuery):
    await callback.answer()
    _, items = _cost_items(callback.from_user.id)
    try:
        idx = int(callback.data.split(":", 1)[1])
        items[idx]
    except (ValueError, IndexError):
        await callback.message.answer("Этот список устарел — пришли отчёт заново.")
        return
    await _ask_cost_for(callback.message, callback.from_user.id, idx)


@dp.callback_query(F.data == "cost_manual")
async def callback_cost_manual(callback: CallbackQuery):
    await callback.answer()
    _cost_state[callback.from_user.id] = {"mode": "manual_article"}
    await callback.message.answer("Напиши артикул товара (как в отчёте). /cancel — отмена")


@dp.callback_query(F.data == "cost_all")
async def callback_cost_all(callback: CallbackQuery):
    await callback.answer()
    _, items = _cost_items(callback.from_user.id)
    if not items:
        await callback.message.answer("Сначала пришли отчёт.")
        return
    hint = ""
    if len(items) > 15:
        hint = f"\n\n💡 Артикулов много ({len(items)}) — удобнее заполнить Excel."
    await callback.message.answer(
        "Как удобнее указать себестоимость?" + hint,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📄 Заполнить в Excel", callback_data="cost_excel"),
             InlineKeyboardButton(text="💬 Написать в чат", callback_data="cost_chat")],
        ]),
    )


def _build_cost_template(snap: dict, saved: dict) -> str:
    import pandas as pd
    items = sorted(snap["items"], key=lambda it: -it["payout"])
    df = pd.DataFrame([{
        "Маркетплейс": snap["marketplace"],
        "Артикул": it["article"],
        "Название": it.get("name", ""),
        "Продано, шт": it["units"],
        COST_TEMPLATE_COL: saved.get(it["article"]),
    } for it in items])
    fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="cost_")
    os.close(fd)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Себестоимость")
        ws = writer.sheets["Себестоимость"]
        for col, width in zip("ABCDE", (12, 26, 40, 12, 22)):
            ws.column_dimensions[col].width = width
    return path


@dp.callback_query(F.data == "cost_excel")
async def callback_cost_excel(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    snap, items = _cost_items(user_id)
    if not items:
        await callback.message.answer("Сначала пришли отчёт.")
        return
    path = _build_cost_template(snap, get_costs(user_id, snap["marketplace"]))
    try:
        _cost_state[user_id] = {"mode": "file"}
        await callback.message.answer_document(
            FSInputFile(path, filename=f"себестоимость_{snap['marketplace']}.xlsx"),
            caption=(
                "📄 Заполни столбец «Себестоимость 1 шт, ₽» и пришли файл обратно.\n"
                "Пустые строки пропущу. /cancel — отмена"
            ),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@dp.callback_query(F.data == "cost_chat")
async def callback_cost_chat(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    snap, items = _cost_items(user_id)
    if not items:
        await callback.message.answer("Сначала пришли отчёт.")
        return
    if len(items) > COST_CHAT_LIMIT:
        await callback.message.answer(
            f"Артикулов слишком много для чата ({len(items)}) — заполни Excel:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📄 Заполнить в Excel", callback_data="cost_excel")]]),
        )
        return
    saved = get_costs(user_id, snap["marketplace"])
    order = sorted(range(len(items)), key=lambda i: -items[i]["payout"])
    lines = ["📋 <b>Напиши себестоимость 1 шт по порядку</b>, через запятую или по одной в строке.",
             "Пропустить товар — <code>-</code>.", ""]
    for n, i in enumerate(order, 1):
        it = items[i]
        name = f" — {_html_escape(it['name'][:40])}" if it.get("name") else ""
        cur = f" <i>(сейчас {_money(saved[it['article']])})</i>" if it["article"] in saved else ""
        lines.append(f"{n}. {_html_escape(it['article'])}{name}{cur}")
    example = ", ".join(["850", "320", "-"][:len(order)])
    lines += ["", f"Например: <code>{example}</code>", "/cancel — отмена"]
    _cost_state[user_id] = {"mode": "list", "order": order}
    for chunk in split_text("\n".join(lines), max_len=4000):
        await callback.message.answer(chunk, parse_mode="HTML")


async def _handle_cost_text(message: Message, st: dict) -> bool:
    """Обрабатывает ввод в режимах себестоимости. True — сообщение обработано."""
    user_id = message.from_user.id
    text = message.text.strip()
    if text.startswith("/"):
        return False
    mode = st.get("mode")

    if mode == "tax_custom":
        m = re.search(r"(\d+(?:[.,]\d+)?)", text)
        if not m:
            await message.answer("Напиши, например: «доходы 4» или «расходы 10». /cancel — отмена")
            return True
        rate = float(m.group(1).replace(",", "."))
        if not 0 < rate <= 20:
            await message.answer("Ставка должна быть от 0 до 20%.")
            return True
        tax_mode = "usn15" if "расход" in text.lower() else "usn6"
        set_tax(user_id, tax_mode, rate)
        _cost_state.pop(user_id, None)
        await message.answer(f"✅ Налог: {P.TaxSettings(tax_mode, rate).label}")
        if get_last_report(user_id):
            await _send_profit(message, user_id)
        return True

    snap, items = _cost_items(user_id)
    if not snap:
        _cost_state.pop(user_id, None)
        await message.answer("Сначала пришли отчёт.")
        return True

    if mode == "manual_article":
        q = text.lower()
        exact = [i for i, it in enumerate(items) if it["article"].lower() == q]
        found = exact or [i for i, it in enumerate(items) if q in it["article"].lower()]
        if not found:
            await message.answer("Не нашёл такой артикул в отчёте. Проверь написание или /cancel.")
            return True
        if len(found) > 1:
            await message.answer(
                "Нашлось несколько — выбери:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=items[i]["article"][:40], callback_data=f"cost_pick:{i}")]
                    for i in found[:10]
                ]),
            )
            return True
        await _ask_cost_for(message, user_id, found[0])
        return True

    if mode == "one_value":
        try:
            cost = P.parse_money(text)
        except ValueError as exc:
            await message.answer(f"⚠️ {exc}. Напиши число, например 850.")
            return True
        if cost is None:
            await message.answer("Нужна цена больше нуля, например 850. /cancel — отмена")
            return True
        it = items[st["idx"]]
        set_costs(user_id, snap["marketplace"], {it["article"]: cost})
        _cost_state.pop(user_id, None)
        tax = _tax_for(user_id)
        await message.answer(
            P.render_item_card(P.item_profit(it, cost, tax), tax),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Другой артикул", callback_data="cost_one"),
                 InlineKeyboardButton(text="📋 Посчитать все", callback_data="cost_all")],
                [InlineKeyboardButton(text=f"🧾 Налог: {tax.label}", callback_data="tax_menu")],
            ]),
        )
        return True

    if mode == "list":
        order = st["order"]
        try:
            parsed = P.parse_cost_list(text, len(order))
        except ValueError as exc:
            await message.answer(f"⚠️ {exc}\nПопробуй ещё раз или /cancel.")
            return True
        costs = {items[order[n]]["article"]: v for n, v in parsed.items()}
        set_costs(user_id, snap["marketplace"], costs)
        _cost_state.pop(user_id, None)
        await message.answer(f"✅ Сохранил себестоимость для {len(costs)} арт.")
        await _send_profit(message, user_id)
        return True

    if mode == "file":
        await message.answer("Жду заполненный Excel-файл с себестоимостью. /cancel — отмена")
        return True
    return False


async def _handle_cost_file(message: Message) -> None:
    """Импорт заполненного шаблона себестоимости."""
    import pandas as pd
    user_id = message.from_user.id
    snap = get_last_report(user_id)
    file_name = message.document.file_name or ""
    ext = os.path.splitext(file_name)[1].lower()
    if ext not in (".xlsx", ".xls", ".csv"):
        await message.answer("Пришли файл .xlsx с себестоимостью (шаблон из бота).")
        return
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="costin_") as tmp:
        tmp_path = tmp.name
    try:
        await bot.download(message.document, destination=tmp_path)
        df = pd.read_csv(tmp_path, sep=None, engine="python") if ext == ".csv" else pd.read_excel(tmp_path)
    except Exception:
        logger.exception("Не удалось прочитать файл себестоимости от %s", user_id)
        await message.answer("😔 Не получилось прочитать файл. Заполни шаблон из бота и пришли снова.")
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    df.columns = [str(c).strip() for c in df.columns]
    cost_col = next((c for c in df.columns if "себестоим" in c.lower()), None)
    if "Артикул" not in df.columns or not cost_col:
        await message.answer("В файле нет столбцов «Артикул» и «Себестоимость 1 шт, ₽». Используй шаблон из бота.")
        return
    by_mp: dict[str, dict[str, float]] = {}
    errors = 0
    default_mp = (snap or {}).get("marketplace", "WB")
    for _, row in df.iterrows():
        art = str(row["Артикул"]).strip()
        raw = row[cost_col]
        if not art or art == "nan" or pd.isna(raw):
            continue
        try:
            v = P.parse_money(str(raw))
        except ValueError:
            errors += 1
            continue
        if v is None:
            continue
        mp = str(row.get("Маркетплейс", default_mp) or default_mp).strip()
        mp = "Ozon" if mp.lower() == "ozon" else ("WB" if mp.upper() in ("WB", "WILDBERRIES") else default_mp)
        by_mp.setdefault(mp, {})[art] = v
    saved = sum(set_costs(user_id, mp, costs) for mp, costs in by_mp.items())
    _cost_state.pop(user_id, None)
    msg = f"✅ Сохранил себестоимость для {saved} арт."
    if errors:
        msg += f"\n⚠️ {errors} строк(и) пропущены: не число в столбце себестоимости."
    await message.answer(msg)
    if snap:
        await _send_profit(message, user_id)


@dp.message(Command("cost"))
async def cmd_cost(message: Message):
    """/cost — список себестоимости; /cost АРТИКУЛ ЦЕНА — изменить; ЦЕНА 0 — удалить."""
    user_id = message.from_user.id
    parts = (message.text or "").strip().split()
    snap = get_last_report(user_id)
    if len(parts) >= 3:
        article = " ".join(parts[1:-1])
        all_costs = get_costs(user_id)
        mp = next((m for m, c in all_costs.items() if article in c), (snap or {}).get("marketplace", "WB"))
        try:
            value = P.parse_money(parts[-1])
        except ValueError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        if value is None:
            ok = delete_cost(user_id, mp, article)
            await message.answer("🗑 Удалил." if ok else "Такого артикула в списке нет.")
        else:
            set_costs(user_id, mp, {article: value})
            await message.answer(f"✅ {article}: {_money(value)} за шт ({mp})")
        return
    all_costs = get_costs(user_id)
    lines = ["💰 <b>Себестоимость товаров</b>", ""]
    if not all_costs:
        lines.append("Пока не указана.")
    for mp, costs in all_costs.items():
        lines.append(f"<b>{mp}</b>")
        for art, c in list(costs.items())[:60]:
            lines.append(f"  {_html_escape(art)} — {_money(c)}")
        if len(costs) > 60:
            lines.append(f"  … и ещё {len(costs) - 60}")
    lines += ["", "Изменить: <code>/cost АРТИКУЛ 850</code>", "Удалить: <code>/cost АРТИКУЛ 0</code>",
              f"🧾 Налог: {_tax_for(user_id).label} — /tax"]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Указать по последнему отчёту", callback_data="cost_menu")]]) if snap else None
    for chunk in split_text("\n".join(lines), max_len=4000):
        await message.answer(chunk, parse_mode="HTML", reply_markup=kb)


def _tax_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="УСН «доходы» 6%", callback_data="tax:usn6:6")],
        [InlineKeyboardButton(text="УСН «доходы − расходы» 15%", callback_data="tax:usn15:15")],
        [InlineKeyboardButton(text="Без налога", callback_data="tax:none:0"),
         InlineKeyboardButton(text="✏️ Другая ставка", callback_data="tax_custom")],
    ])


TAX_HELP = (
    "🧾 <b>Система налогообложения</b>\n\n"
    "• <b>УСН «доходы»</b> — налог с выручки (цены продажи покупателю). Обычно 6%, в ряде регионов меньше.\n"
    "• <b>УСН «доходы − расходы»</b> — налог с разницы: к выплате минус себестоимость. Обычно 15%.\n\n"
    "Это оценка за период отчёта — точную сумму считает бухгалтер по итогам года."
)


@dp.message(Command("tax"))
async def cmd_tax(message: Message):
    await message.answer(
        TAX_HELP + f"\n\nСейчас: <b>{_tax_for(message.from_user.id).label}</b>",
        parse_mode="HTML", reply_markup=_tax_keyboard(),
    )


@dp.callback_query(F.data == "tax_menu")
async def callback_tax_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        TAX_HELP + f"\n\nСейчас: <b>{_tax_for(callback.from_user.id).label}</b>",
        parse_mode="HTML", reply_markup=_tax_keyboard(),
    )


@dp.callback_query(F.data.startswith("tax:"))
async def callback_tax_set(callback: CallbackQuery):
    await callback.answer()
    try:
        _, mode, rate = callback.data.split(":")
        if mode not in P.TAX_MODES:
            raise ValueError
        rate = float(rate)
    except ValueError:
        return
    user_id = callback.from_user.id
    set_tax(user_id, mode, rate)
    await callback.message.answer(f"✅ Налог: {P.TaxSettings(mode, rate).label}")
    snap = get_last_report(user_id)
    if snap and get_costs(user_id, snap["marketplace"]):
        await _send_profit(callback.message, user_id)


@dp.callback_query(F.data == "tax_custom")
async def callback_tax_custom(callback: CallbackQuery):
    await callback.answer()
    _cost_state[callback.from_user.id] = {"mode": "tax_custom"}
    await callback.message.answer(
        "Напиши режим и ставку, например:\n• <code>доходы 4</code>\n• <code>расходы 10</code>\n/cancel — отмена",
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK-ХЕНДЛЕРЫ
# ═══════════════════════════════════════════════════════════════════════════════

# (Обработчик pay_card удален)


@dp.callback_query(F.data.startswith("feedback_stars_"))
async def callback_feedback_stars(callback: CallbackQuery):
    """Обработчик выбора звёзд."""
    await callback.answer()
    stars = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    _pending_feedback[user_id] = stars

    # Убираем клавиатуру
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    stars_display = "★" * stars
    await callback.message.answer(
        f"Вы поставили оценку <b>{stars} / 5</b>  {stars_display}\n\n"
        f"✏️ Напишите отзыв одним сообщением (что понравилось, "
        f"что улучшить) или нажмите /skip чтобы пропустить.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "feedback_skip")
async def callback_feedback_skip(callback: CallbackQuery):
    """Пропуск обратной связи."""
    await callback.answer("Спасибо! 😊")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Спасибо! Пришлёте отчёт — проанализирую. 📊")


async def _drop_pressed_button(callback: CallbackQuery) -> None:
    """Убирает из-под отчёта только нажатую кнопку (остальные действия остаются)."""
    try:
        markup = callback.message.reply_markup
        rows = [
            [b for b in row if getattr(b, "callback_data", None) != callback.data]
            for row in (markup.inline_keyboard if markup else [])
        ]
        rows = [r for r in rows if r]
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )
    except Exception:
        pass


@dp.callback_query(F.data == "post_review")
async def callback_post_review(callback: CallbackQuery):
    """Кнопка '⭐ Оставить отзыв' под отчётом → открывает клавиатуру звёзд."""
    await callback.answer()
    await _drop_pressed_button(callback)
    await callback.message.answer(
        "🙏 <b>Спасибо, что хотите оставить отзыв!</b>\n\n"
        "Сколько звёзд вы поставите работе бота?",
        parse_mode="HTML",
        reply_markup=get_feedback_keyboard(),
    )


@dp.callback_query(F.data == "post_problem")
async def callback_post_problem(callback: CallbackQuery):
    """
    Кнопка '🚨 Сообщить о проблеме' → ставим пользователя в режим
    ожидания жалобы. Следующее его сообщение летит админу.
    """
    await callback.answer()
    user_id = callback.from_user.id
    await _drop_pressed_button(callback)
    # Ждём жалобу; привязываем её к последнему файлу пользователя
    _pending_problem[user_id] = _last_file_name.get(user_id, "неизвестно")
    await callback.message.answer(
        "🚨 <b>Опишите проблему одним сообщением</b>\n\n"
        "Например:\n"
        "• «не нашёл артикул ABC-123»\n"
        "• «сумма убытка не сходится»\n"
        "• «нет колонки с доходами»\n\n"
        "📎 К сообщению можно приложить скриншот.\n"
        "Все данные (файл + ваше описание) получит разработчик.\n\n"
        "Отправьте /cancel чтобы выйти.",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "post_ai")
async def callback_post_ai(callback: CallbackQuery):
    """
    Кнопка '🤖 AI-разбор' → запрашивает LLM-саммари по последнему отчёту.
    Если AI не настроен — отдаёт понятное сообщение.
    """
    await callback.answer()
    user_id = callback.from_user.id

    ai = get_ai_client()
    if not ai.available:
        await callback.message.answer(
            AI_UNAVAILABLE_TEXT,
            parse_mode="HTML",
        )
        return

    last = _last_reports.get(user_id)
    if not last:
        await callback.message.answer(
            "Сначала пришлите свежий отчёт — AI будет анализировать его."
        )
        return

    await _drop_pressed_button(callback)

    thinking = await callback.message.answer("🤖 AI думает… ⏳")

    try:
        summary = await ai_summarize_report(
            report_text=last["text"],
            marketplace=last["marketplace"],
        )
        await thinking.edit_text(
            f"🤖 <b>AI-разбор:</b>\n\n{_html_escape(summary)}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.exception("AI-сбой для user=%s: %s", user_id, exc)
        await thinking.edit_text(
            "🤖 Не удалось получить AI-разбор. Попробуй позже."
        )


@dp.callback_query(F.data == "post_repeat")
async def callback_post_repeat(callback: CallbackQuery):
    """
    Кнопка '🔄 Повторить анализ' → удаляет кнопки и предлагает прислать файл снова.
    (Сам файл мы не храним — это by design, ради приватности.)
    """
    await callback.answer()
    await _drop_pressed_button(callback)
    await callback.message.answer(
        "🔄 Окей, пришли мне файл ещё раз — повторю анализ.\n\n"
        "💡 <i>Совет:</i> хочешь сравнить с прошлой неделей? "
        "Напиши /compare и пришли два файла.",
        parse_mode="HTML",
    )


@dp.message(Command("skip"))
async def cmd_skip_feedback(message: Message):
    """Пропуск текстового отзыва."""
    user_id = message.from_user.id
    if user_id in _pending_feedback:
        stars = _pending_feedback.pop(user_id)
        save_feedback(user_id, stars, "")
        await message.answer("✅ Спасибо за оценку! Пришлёте отчёт — проанализирую. 📊")
    else:
        await message.answer(
            "Отправь мне детализированный отчёт WB или Ozon "
            "в формате Excel (.xlsx) или CSV (.csv)."
        )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Отмена любого pending-состояния (отзыв / жалоба)."""
    user_id = message.from_user.id
    cancelled = False
    if user_id in _pending_feedback:
        _pending_feedback.pop(user_id, None)
        cancelled = True
    if user_id in _pending_problem:
        _pending_problem.pop(user_id, None)
        cancelled = True
    if user_id in _compare_state:
        _compare_state.pop(user_id, None)
        cancelled = True
    if user_id in _cost_state:
        _cost_state.pop(user_id, None)
        cancelled = True
    if cancelled:
        await message.answer("🚫 Действие отменено. Отправьте новый отчёт в любой момент.")
    else:
        await message.answer("Нечего отменять 🙂")


# ═══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТА (catch-all)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message()
async def handle_other(message: Message):
    """Обработчик любых других сообщений (текст, фото)."""
    user_id = message.from_user.id

    # ── Если ждём жалобу от этого пользователя ──────────────────────
    if user_id in _pending_problem:
        file_name = _pending_problem.pop(user_id)
        # Сохраняем факт жалобы в БД (чтобы админ видел историю)
        try:
            save_feedback(user_id, 0, f"[ПРОБЛЕМА по файлу {file_name}] {message.text or ''}")
        except Exception:
            pass

        # Пересылаем админу
        if ADMIN_ID:
            try:
                username = message.from_user.username or "—"
                admin_text = (
                    f"🚨 <b>Жалоба от пользователя</b>\n\n"
                    f"От: @{_html_escape(username)} (<code>{user_id}</code>)\n"
                    f"Файл (последний): <code>{_html_escape(file_name)}</code>\n\n"
                    f"📝 <b>Текст жалобы:</b>\n"
                    f"<pre>{_html_escape(message.text or message.caption or '(пусто)')}</pre>"
                )
                await bot.send_message(
                    ADMIN_ID,
                    admin_text,
                    parse_mode="HTML",
                )
                # Если пользователь прислал фото — пересылаем его тоже
                if message.photo:
                    try:
                        await bot.send_photo(
                            ADMIN_ID,
                            message.photo[-1].file_id,
                            caption=f"📸 Скриншот от @{username}",
                        )
                    except Exception:
                        pass
            except Exception:
                logger.exception("Не удалось переслать жалобу админу")

        await message.answer(
            "✅ Спасибо! Жалоба отправлена разработчику.\n"
            "Он получит ваше описание и файл отчёта для разбора.\n\n"
            "Можете продолжать пользоваться ботом.",
        )
        logger.info("Жалоба от %s по файлу %s: %s", user_id, file_name, (message.text or "")[:100])
        return

    # Ввод себестоимости / ставки налога
    if user_id in _cost_state and isinstance(message.text, str):
        if await _handle_cost_text(message, _cost_state[user_id]):
            return

    # Если ждём текст отзыва от этого пользователя
    if user_id in _pending_feedback:
        stars = _pending_feedback.pop(user_id)
        text = message.text or ""
        save_feedback(user_id, stars, text)

        stars_display = "★" * stars
        await message.answer(
            f"✅ Спасибо за отзыв!\n\n"
            f"Оценка: <b>{stars} / 5</b>  {stars_display}\n"
            f"Отзыв: {_html_escape(text[:200])}\n\n"
            f"Ваше мнение очень ценно! 🙏",
            parse_mode="HTML",
        )

        # Уведомляем админа
        if ADMIN_ID:
            try:
                username = message.from_user.username or "—"
                await bot.send_message(
                    ADMIN_ID,
                    f"📝 <b>Новый отзыв</b>\n\n"
                    f"От: @{_html_escape(username)} ({user_id})\n"
                    f"Оценка: <b>{stars} / 5</b>  {stars_display}\n"
                    f"Текст: {_html_escape(text[:500])}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        logger.info("Отзыв от %s: %s звёзд, текст: %s", user_id, stars, text[:100])
        return

    # Обычное сообщение — подсказка
    await message.answer(
        "📄 Отправь мне детализированный отчёт WB или Ozon "
        "в формате Excel (.xlsx) или CSV (.csv).\n\n"
        "Команды: /compare /buy /help"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════════

def split_text(text: str, max_len: int = 4000) -> list[str]:
    """Разбивает длинный текст на части по строкам."""
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def _html_escape(text: str) -> str:
    """Экранирует HTML для безопасной вставки в <pre>...</pre>."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


async def main():
    logger.info("Бот запускается...")
    logger.info("ADMIN_ID: %s", ADMIN_ID)
    if ym.enabled():
        asyncio.create_task(_yoomoney_poller())
    try:
        await legal.ensure_published()
    except Exception as exc:
        logger.warning("Оферта: не удалось опубликовать на telegra.ph (%s) — /terms покажет текст в чате", exc)
    logger.info("Чеки «Мой налог»: %s", "автоматически" if NR.auto_enabled() else "вручную (/receipt)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
