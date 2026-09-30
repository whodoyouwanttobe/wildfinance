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
import json
import tempfile
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
)
from wb_parser import analyze as wb_analyze
from ozon_parser import analyze as ozon_analyze
from parser_dispatcher import analyze, detect_marketplace, compute
from report_compare import compare_metrics, compare_plain
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
PRICE_FOREVER_RUB = int(os.getenv("PRICE_FOREVER_RUB", "5000"))
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
}

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
    "  /help — помощь"
)

TRIAL_EXPIRED_TEXT = (
    "⏱ <b>Бесплатный период закончился.</b>\n\n"
    "Получи полный доступ:\n"
    "  💎 Вечный доступ — 5000 руб.\n"
    "  📆 Подписка — 1490 руб/мес.\n\n"
    "Нажми /buy для оплаты."
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
    "  /ai — AI-разбор последнего отчёта\n"
    "  /buy — тарифы и оплата\n"
    "  /start — начать заново"
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
            [KeyboardButton(text="📈 Моя статистика")],
            [
                KeyboardButton(text="💳 Оплатить доступ"),
                KeyboardButton(text="🎁 Пригласить друга"),
            ],
            [KeyboardButton(text="📄 Помощь")],
        ],
        resize_keyboard=True,
    )

def get_buy_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура оплаты.
    Если задан PAYMENT_PROVIDER_TOKEN — кнопки выставляют счёт внутри Telegram
    (автовыдача доступа). Иначе — старые ссылки ЮKassa (ручная выдача).
    """
    m, f = PLANS["month"]["price_rub"], PLANS["forever"]["price_rub"]
    if PAYMENT_PROVIDER_TOKEN:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 1 месяц — {m} руб.", callback_data="buy_month")],
            [InlineKeyboardButton(text=f"💎 Навсегда — {f} руб.", callback_data="buy_forever")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 1 месяц — {m} руб.", url=PAYMENT_URL_MONTH)],
        [InlineKeyboardButton(text=f"💎 Навсегда — {f} руб.", url=PAYMENT_URL_FOREVER)],
    ])


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

@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start."""
    user_id = message.from_user.id
    username = message.from_user.username or ""

    is_new = add_user(user_id, username)
    if is_new:
        logger.info("Новый пользователь: %s (@%s)", user_id, username)

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
    text = (
        "💎 <b>Тарифы:</b>\n\n"
        f"  🔹 <b>Вечный доступ</b> — {PLANS['forever']['price_rub']} руб. (разово)\n"
        "     Все обновления. Без ограничений.\n\n"
        f"  🔹 <b>Месяц</b> — {PLANS['month']['price_rub']} руб.\n"
        "     30 дней доступа. Без автосписаний — продлеваешь сам.\n\n"
    )
    if PAYMENT_PROVIDER_TOKEN:
        text += "Оплата картой прямо в Telegram, доступ откроется автоматически:"
    else:
        text += (
            "После оплаты пришли сюда чек — доступ откроем вручную.\n"
            "Выбери тариф:"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=get_buy_keyboard())


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


def _parse_payload(payload: str) -> tuple[str, int] | None:
    """'month:123' → ('month', 123). None, если payload чужой/битый."""
    try:
        plan_key, uid = payload.split(":", 1)
        if plan_key not in PLANS:
            return None
        return plan_key, int(uid)
    except (ValueError, AttributeError):
        return None


@dp.callback_query(F.data.in_({"buy_month", "buy_forever"}))
async def callback_buy(callback: CallbackQuery):
    """Кнопка тарифа → счёт на оплату внутри Telegram."""
    await callback.answer()
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer("Оплата временно недоступна. Напишите /buy позже.")
        return
    plan_key = callback.data.removeprefix("buy_")
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

    add_user(user_id, message.from_user.username or "")
    is_new = record_payment(
        charge_id=sp.telegram_payment_charge_id,
        user_id=user_id,
        plan=plan_key,
        amount=sp.total_amount,
        provider_charge_id=sp.provider_payment_charge_id or "",
    )
    if not is_new:
        logger.warning("Повторный successful_payment %s — пропускаю", sp.telegram_payment_charge_id)
        return

    until = extend_access(user_id, plan["days"])
    until_text = "навсегда" if plan_key == "forever" else f"до {until[:10]}"
    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\nДоступ открыт {until_text}.\n"
        "Присылай отчёт — разберу его за секунды.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(),
    )
    logger.info("Оплата: user=%s plan=%s amount=%s", user_id, plan_key, sp.total_amount)

    if ADMIN_ID:
        try:
            uname = message.from_user.username or "—"
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Новая оплата</b>\n\n"
                f"@{_html_escape(uname)} (<code>{user_id}</code>)\n"
                f"Тариф: {plan['title']}\n"
                f"Сумма: {sp.total_amount / 100:.0f} руб.\n"
                f"ЮKassa ID: <code>{_html_escape(sp.provider_payment_charge_id or '—')}</code>",
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Не удалось уведомить админа об оплате")


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
    link = f"https://t.me/{me.username}" if me and getattr(me, "username", None) else "https://t.me/wildfinance_bot"
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
        await message.answer(TRIAL_EXPIRED_TEXT, parse_mode="HTML", reply_markup=get_buy_keyboard())
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
        result = await asyncio.to_thread(analyze, tmp_path)

        # Логируем, какой маркетплейс был определён (полезно для статистики)
        try:
            marketplace = detect_marketplace(tmp_path)
            logger.info(
                "Маркетплейс=%s, файл='%s', user=%s",
                marketplace, file_name, user_id,
            )
        except Exception:
            marketplace = "?"

        # Удаляем «анализирую...»
        try:
            await processing_msg.delete()
        except Exception:
            pass

        # Отправляем результат (HTML, не <pre>, т.к. внутри уже HTML-теги)
        if len(result) <= 4096:
            await message.answer(result, parse_mode="HTML")
        else:
            chunks = split_text(result, max_len=4000)
            for chunk in chunks:
                await message.answer(chunk, parse_mode="HTML")

        # Запоминаем имя файла, чтобы привязать возможную жалобу к отчёту
        _last_file_name[user_id] = file_name

        # Сохраняем результат последнего отчёта (для AI-кнопки и сравнений)
        _last_reports[user_id] = {
            "text": result,
            "marketplace": marketplace if marketplace != "?" else "WB",
            "file_name": file_name,
            "timestamp": __import__("time").time(),
        }

        await message.answer(
            "Что дальше?",
            reply_markup=get_post_report_keyboard(),
        )

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
        await message.answer(TRIAL_EXPIRED_TEXT, parse_mode="HTML", reply_markup=get_buy_keyboard())
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
    try:
        text = compare_metrics(old, metrics)
    except ValueError as exc:
        await message.answer(
            f"⚠️ {exc}\n\nПришли новый отчёт того же маркетплейса ({old['marketplace']}) или /cancel."
        )
        return

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


@dp.callback_query(F.data == "post_review")
async def callback_post_review(callback: CallbackQuery):
    """Кнопка '⭐ Оставить отзыв' под отчётом → открывает клавиатуру звёзд."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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
    # Убираем кнопки
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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

    # Убираем кнопки, чтобы не было двойного клика
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

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
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
