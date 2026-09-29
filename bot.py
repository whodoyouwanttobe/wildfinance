"""
bot.py — Асинхронный Telegram-бот на aiogram 3.x.

Экспресс-аудитор отчётов Wildberries.

Функции:
  - /start — приветствие + регистрация
  - Загрузка .xlsx/.csv → финансовый анализ (HTML с эмодзи)
  - /buy — кнопки оплаты (ЮKassa + перевод на карту)
  - /give_access <user_id> <дней> — админ-команда (только для ADMIN_ID)
  - Обратная связь: звёзды + текст после первого отчёта

Переменные окружения:
  BOT_TOKEN  — токен Telegram-бота от @BotFather
  ADMIN_ID   — Telegram ID администратора (для /give_access)
  PAYMENT_URL — ссылка на оплату (ЮKassa / Telegram Stars)

Запуск:
  python bot.py
"""

import os
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
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from database import (
    add_user,
    is_trial_active,
    grant_access,
    increment_report_count,
    save_feedback,
    has_given_feedback,
)
from wb_parser import analyze

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─── Конфигурация ────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PAYMENT_URL = os.getenv("PAYMENT_URL", "https://example.com/pay")
CARD_DETAILS = os.getenv("CARD_DETAILS", "Сбербанк: 0000 0000 0000 0000 (Имя Ф.)")

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
    "👋 <b>Привет! Я экспресс-аудитор отчетов WB.</b>\n\n"
    "У тебя есть <b>7 дней бесплатного доступа</b>.\n\n"
    "📄 Отправь мне еженедельный детализированный отчет "
    "(Excel или CSV), и я за секунду посчитаю:\n\n"
    "  💰 Реальную чистую прибыль\n"
    "  🔴 Скрытые штрафы и переплаты\n"
    "  ⚠️ ТОП убыточных товаров\n"
    "  💡 Рекомендации по оптимизации\n\n"
    "Команды:\n"
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
    "1️⃣ Скачай детализированный отчёт WB:\n"
    "   Личный кабинет → Финансы → Детализация\n\n"
    "2️⃣ Пришли файл (.xlsx или .csv) в этот чат\n\n"
    "3️⃣ Получи полный анализ за секунды:\n"
    "   • Чистая прибыль по каждому артикулу\n"
    "   • Скрытые расходы и штрафы\n"
    "   • Рекомендации по оптимизации\n\n"
    "📌 Команды:\n"
    "  /buy — тарифы и оплата\n"
    "  /start — начать заново"
)

SUPPORTED_EXTENSIONS = (".xlsx", ".xls", ".csv")

# ─── Тексты ошибок ────────────────────────────────────────────────────────────

ERROR_WRONG_FORMAT = (
    "⚠️ Не могу прочитать этот файл.\n\n"
    "Убедись, что это именно детализированный еженедельный отчёт WB:\n"
    "  Личный кабинет → Финансы → Детализация\n\n"
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
# INLINE-КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════════

def get_buy_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура оплаты: подписка и навсегда."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Подписка на месяц (1490 руб.)",
            url=PAYMENT_URL,
        )],
        [InlineKeyboardButton(
            text="💳 Навсегда / Lifetime (5000 руб.)",
            url=PAYMENT_URL,
        )]
    ])


def get_feedback_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора звёзд для отзыва."""
    stars_row = [
        InlineKeyboardButton(text=f"{'⭐' * i}", callback_data=f"feedback_stars_{i}")
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        stars_row,
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data="feedback_skip")],
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

    await message.answer(WELCOME_TEXT, parse_mode="HTML")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help."""
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    """Обработчик команды /buy — показывает варианты оплаты."""
    text = (
        "💎 <b>Тарифы:</b>\n\n"
        "  🔹 <b>Вечный доступ</b> — 5000 руб. (разово)\n"
        "     Все обновления. Без ограничений.\n\n"
        "  🔹 <b>Подписка</b> — 1490 руб/мес.\n"
        "     Автопродление. Отмена в любой момент.\n\n"
        "Выбери удобный способ оплаты:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_buy_keyboard())


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


# ═══════════════════════════════════════════════════════════════════════════════
# ХЕНДЛЕР ДОКУМЕНТОВ
# ═══════════════════════════════════════════════════════════════════════════════

# Словарь для хранения состояния ожидания отзыва {user_id: stars}
_pending_feedback: dict[int, int] = {}


@dp.message(F.document)
async def handle_document(message: Message):
    """Обработчик входящих файлов (.xlsx, .xls, .csv)."""
    user_id = message.from_user.id
    username = message.from_user.username or ""

    # Регистрируем пользователя, если ещё не в БД
    add_user(user_id, username)

    # Проверяем доступ (триал или оплаченный)
    if not is_trial_active(user_id):
        await message.answer(TRIAL_EXPIRED_TEXT, parse_mode="HTML")
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

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.info("Временный файл удалён: %s", tmp_path)
            except OSError as e:
                logger.warning("Не удалось удалить файл %s: %s", tmp_path, e)


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

    stars_display = "⭐" * stars
    await callback.message.answer(
        f"Вы поставили: {stars_display}\n\n"
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
            "Отправь мне детализированный отчёт WB "
            "в формате Excel (.xlsx) или CSV (.csv)."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ ОБРАБОТЧИК ТЕКСТА (catch-all)
# ═══════════════════════════════════════════════════════════════════════════════

@dp.message()
async def handle_other(message: Message):
    """Обработчик любых других сообщений (текст, фото)."""
    user_id = message.from_user.id

    # Если ждём текст отзыва от этого пользователя
    if user_id in _pending_feedback:
        stars = _pending_feedback.pop(user_id)
        text = message.text or ""
        save_feedback(user_id, stars, text)

        stars_display = "⭐" * stars
        await message.answer(
            f"✅ Спасибо за отзыв!\n\n"
            f"Оценка: {stars_display}\n"
            f"Отзыв: {text[:200]}\n\n"
            f"Ваше мнение очень ценно! 🙏",
        )

        # Уведомляем админа
        if ADMIN_ID:
            try:
                username = message.from_user.username or "—"
                await bot.send_message(
                    ADMIN_ID,
                    f"📝 <b>Новый отзыв</b>\n\n"
                    f"От: @{username} ({user_id})\n"
                    f"Оценка: {stars_display}\n"
                    f"Текст: {text[:500]}",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        logger.info("Отзыв от %s: %s звёзд, текст: %s", user_id, stars, text[:100])
        return

    # Обычное сообщение — подсказка
    await message.answer(
        "📄 Отправь мне детализированный отчёт WB "
        "в формате Excel (.xlsx) или CSV (.csv).\n\n"
        "Команды: /buy /help"
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


async def main():
    logger.info("Бот запускается...")
    logger.info("ADMIN_ID: %s", ADMIN_ID)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
