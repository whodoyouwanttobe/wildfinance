"""
smoke_test_ai.py — Полная проверка AI-интеграции после добавления ключей.

Что проверяет:
  1) Все провайдеры (OpenAI, OpenRouter, YandexGPT, GigaChat)
  2) Все 3 tier-модели с реальными запросами
  3) Автоматический fallback между tiers
  4) Все 3 функции (summarize, compare, chat)
  5) Корректность возвращаемых данных

Запуск: python smoke_test_ai.py
"""

import asyncio
import sys
import os

# Включаем UTF-8 для Windows-консоли
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# Загружаем .env ДО импорта ai_client
from dotenv import load_dotenv
load_dotenv()

print("=" * 60)
print("[CHECK] ПРОВЕРКА AI-ИНТЕГРАЦИИ")
print("=" * 60)

# 1. Проверяем зависимости
print("\n[DOC] Зависимости:")
try:
    import openai
    print(f"  [OK] openai: {openai.__version__}")
except ImportError:
    print("  [ERR] openai НЕ установлен -> pip install openai")

try:
    import httpx
    print(f"  [OK] httpx: {httpx.__version__}")
except ImportError:
    print("  [ERR] httpx НЕ установлен -> pip install httpx")

# 2. Проверяем ключи в .env
print("\n[KEYS] API-ключи в .env:")
keys = {
    "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY"),
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    "YANDEX_API_KEY": os.getenv("YANDEX_API_KEY"),
    "YANDEX_FOLDER_ID": os.getenv("YANDEX_FOLDER_ID"),
    "GIGACHAT_CREDENTIALS": os.getenv("GIGACHAT_CREDENTIALS"),
}
for k, v in keys.items():
    if v and v not in ("", "sk-proj-...", "AQVN...", "...", "b1g..."):
        masked = v[:8] + "..." + v[-4:] if len(v) > 12 else "***"
        print(f"  [OK]   {k}: {masked}")
    else:
        print(f"  [----] {k}: (не задан)")

# 3. Конфигурация tiers
print("\n[CONFIG] Конфигурация tiers:")
tiers = {
    "AI_TIER1": os.getenv("AI_TIER1", "openai:gpt-4o-mini"),
    "AI_TIER2": os.getenv("AI_TIER2", "openai:gpt-4o"),
    "AI_TIER3": os.getenv("AI_TIER3", ""),
}
for k, v in tiers.items():
    print(f"  {k}: {v or '(пусто)'}")

# Импортируем ai_client
try:
    from ai_client import (
        AIClient, get_ai_client,
        ai_summarize_report, ai_compare_reports, ai_chat,
    )
    print("\n[OK] ai_client.py импортирован успешно")
except Exception as exc:
    print(f"\n[ERR] Ошибка импорта ai_client: {exc}")
    sys.exit(1)

# 4. Проверяем доступность клиентов
print("\n[STATUS] Доступные провайдеры:")
client = get_ai_client()
print(f"  Tier 1 ({tiers['AI_TIER1']}): {'[OK]' if client.tier1 else '[--]'}")
print(f"  Tier 2 ({tiers['AI_TIER2']}): {'[OK]' if client.tier2 else '[--]'}")
print(f"  Tier 3 ({tiers['AI_TIER3'] or '(пусто)'}): {'[OK]' if client.tier3 else '[--]'}")
print(f"  Общий статус available: {'[OK]' if client.available else '[ERR] нет'}")

if not client.available:
    print("\n[ERR] Ни один AI-провайдер не настроен!")
    print("   Добавьте хотя бы один ключ в .env")
    sys.exit(1)

# Тестовый отчёт
SAMPLE_REPORT = """ОТЧЁТ OZON
Выручка (продажи):      +22,500.00 руб.
Логистика (вся):         -3,060.00 руб.
Штрафы и удержания:      -450.00 руб.
Эквайринг:               -315.00 руб.
Услуги партнёров:         -720.00 руб.
ЧИСТАЯ ПРИБЫЛЬ: 17,955.00 руб.

ОБНАРУЖЕНЫ АНОМАЛИИ
! 2 отправлений с обратной логистикой без продаж.

ПРИБЫЛЬ ПО АРТИКУЛАМ (TOP-15)
SKU:SKU-1001: доход 7500, логистика 585, итого 6 802.50
SKU:SKU-2002: доход 7500, drop-off 2400, итого 4 987.50
SKU:SKU-3003: доход 3600, продвижение 720, итого 2 826.00
SKU:SKU-5005: доход 1500, штраф 450, итого 1 050.00"""

OLD_REPORT = """ОТЧЁТ OZON (неделей ранее)
Выручка: 20,000 руб
ЧИСТАЯ ПРИБЫЛЬ: 15,000 руб
SKU:SKU-1001: итого 6 000
SKU:SKU-2002: итого 5 500
SKU:SKU-5005: итого 800
(покатушек не было)"""


async def test_tier(spec: str, tier_name: str) -> bool:
    """Тестирует один tier с реальным запросом."""
    from ai_client import _parse_tier, _get_tier

    tier = _get_tier(spec)
    if tier is None:
        print(f"  ⚪ {tier_name}: не настроен (пропускаем)")
        return True

    print(f"\n  [TEST] Тестируем {tier_name}: {spec}")
    provider, model = _parse_tier(spec)
    print(f"     Провайдер: {provider}, модель: {model}")

    client_obj, model_name = tier

    try:
        # Проверяем: это функция (yandex/gigachat) или OpenAI-клиент
        if client_obj in (
            __import__("ai_client")._ask_yandex,
            __import__("ai_client")._ask_gigachat,
        ):
            result = await client_obj(
                prompt="Скажи 'OK' одним словом",
                system="Ты тестовый ассистент.",
                max_tokens=10,
                model=model_name,
            )
        else:
            from ai_client import _ask_openai_compatible
            result = await _ask_openai_compatible(
                prompt="Скажи 'OK' одним словом",
                system="Ты тестовый ассистент.",
                max_tokens=10,
                model=model_name,
                _client=client_obj,
            )

        if result and len(result.strip()) > 0:
            print(f"     [OK] Ответ получен: '{result.strip()[:50]}'")
            return True
        else:
            print(f"     [ERR] Пустой ответ")
            return False

    except Exception as exc:
        print(f"     [ERR] {type(exc).__name__}: {str(exc)[:150]}")
        return False


async def test_summarize() -> bool:
    """Тестирует simple_summarize()."""
    print("\n[TEST 1] ai_summarize_report() (Tier 1 -> Tier 2)")
    try:
        result = await ai_summarize_report(SAMPLE_REPORT, "Ozon")
        if result and "AI-анализ временно недоступен" in result:
            print(f"  [ERR] Все tiers упали")
            return False
        if result and len(result.strip()) > 20:
            print(f"  [OK] Саммари получен ({len(result)} символов):")
            print(f"     '{result[:200]}...'")
            return True
        else:
            print(f"  [ERR] Пустой или слишком короткий ответ")
            return False
    except Exception as exc:
        print(f"  [ERR] Exception: {exc}")
        return False


async def test_compare() -> bool:
    """Тестирует ai_compare_reports()."""
    print("\n[TEST 2] ai_compare_reports() (Tier 2 -> Tier 3)")
    try:
        result = await ai_compare_reports(OLD_REPORT, SAMPLE_REPORT, "Ozon")
        if result and "AI-анализ временно недоступен" in result:
            print(f"  [ERR] Все tiers упали")
            return False
        if result and len(result.strip()) > 50:
            print(f"  [OK] Сравнение получено ({len(result)} символов):")
            print(f"     '{result[:200]}...'")
            return True
        else:
            print(f"  [ERR] Пустой ответ")
            return False
    except Exception as exc:
        print(f"  [ERR] Exception: {exc}")
        return False


async def test_chat() -> bool:
    """Тестирует ai_chat()."""
    print("\n[TEST 3] ai_chat() (Tier 1 -> Tier 2)")
    try:
        result = await ai_chat(
            SAMPLE_REPORT,
            "Что делать с SKU-2002, у которого drop-off 32%?",
            "Ozon",
        )
        if result and "AI-анализ временно недоступен" in result:
            print(f"  [ERR] Все tiers упали")
            return False
        if result and len(result.strip()) > 20:
            print(f"  [OK] Ответ на вопрос получен ({len(result)} символов):")
            print(f"     '{result[:200]}...'")
            return True
        else:
            print(f"  [ERR] Пустой ответ")
            return False
    except Exception as exc:
        print(f"  [ERR] Exception: {exc}")
        return False


async def test_fallback() -> bool:
    """Проверяет автоматический fallback."""
    print("\n[TEST 4] Автоматический fallback")
    from ai_client import AIClient

    class BrokenClient:
        """Имитация сломанного OpenAI-клиента."""
        class _BrokenChat:
            class completions:
                @staticmethod
                async def create(*args, **kwargs):
                    raise RuntimeError("Tier 1 сломан (имитация)")

        @property
        def chat(self):
            return self._BrokenChat()

    bad_client = AIClient()
    bad_client.tier1 = (BrokenClient(), "broken-model")

    try:
        result = await bad_client.simple_summarize(SAMPLE_REPORT, "Ozon")
        if result and "AI-анализ временно недоступен" not in result:
            print(f"  [OK] Fallback работает — Tier 1 упал, Tier 2 сработал")
            print(f"     '{result[:150]}...'")
            return True
        else:
            print(f"  [ERR] Fallback не сработал (получено: '{result[:80]}')")
            return False
    except Exception as exc:
        print(f"  [ERR] Exception при fallback: {type(exc).__name__}: {exc}")
        return False


async def main():
    print("\n" + "=" * 60)
    print(">>> ЗАПУСК ТЕСТОВ <<<")
    print("=" * 60)

    results = {}

    print("\n[STEP 1] Прямое тестирование каждого tier")
    results["tier1"] = await test_tier(tiers["AI_TIER1"], "Tier 1")
    results["tier2"] = await test_tier(tiers["AI_TIER2"], "Tier 2")
    results["tier3"] = await test_tier(tiers["AI_TIER3"] or "openai:none", "Tier 3")

    print("\n[STEP 2] Тестирование функций (с реальными отчётами)")
    results["summarize"] = await test_summarize()
    results["compare"] = await test_compare()
    results["chat"] = await test_chat()

    print("\n[STEP 3] Тест автоматического fallback")
    results["fallback"] = await test_fallback()

    print("\n" + "=" * 60)
    print("[RESULTS]")
    print("=" * 60)

    total = len(results)
    passed = sum(1 for v in results.values() if v)

    for name, ok in results.items():
        status = "[OK]" if ok else "[ERR]"
        print(f"  {status} {name}")

    print(f"\n  Итого: {passed}/{total}")

    if passed == total:
        print("\n*** ВСЁ РАБОТАЕТ! AI готов к использованию. ***")
    elif passed >= total - 1:
        print("\n[~] Работает, но есть мелкие проблемы (1 тест провален).")
    else:
        print("\n[ERR] Есть серьёзные проблемы, проверьте ключи и настройки.")

    print("\n[TIPS] Что делать если что-то не работает:")
    if not results.get("tier1"):
        print("  - Tier 1 не отвечает -> проверьте ключ OPENROUTER_API_KEY")
        print("    и доступность модели на https://openrouter.ai/models")
    if not results.get("tier2"):
        print("  - Tier 2 не отвечает -> та же причина, проверьте модель")
    if not results.get("summarize"):
        print("  - Саммари не работает -> смотрите логи бота")


if __name__ == "__main__":
    asyncio.run(main())
