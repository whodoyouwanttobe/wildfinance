"""
ai_client.py — Мультипровайдерный LLM-клиент для аналитики отчётов WB/Ozon.

Поддерживает 4 провайдера с автоматическим fallback:
  - OpenAI       (gpt-4o-mini / gpt-4o, прямой API)
  - OpenRouter   (агрегатор: gemini, claude, gpt, deepseek и т.д.)
  - YandexGPT    (через REST, оплата в рублях)
  - GigaChat     (Сбер, рубли)

Архитектура с tier-выбором МОДЕЛИ (не провайдера):
  Tier 1 (быстрые саммари):     gemini-3.7-flash-low / gpt-5.6-luna
  Tier 2 (AI-чат, сравнения):   claude-sonnet-4-6 / gpt-6-sol
  Tier 3 (сложные случаи):      claude-haiku-4-5 / gpt-5.6-sol

Каждая tier указывает пару (провайдер + конкретная_модель).
Если Tier 1 падает → Tier 2 → Tier 3 → сообщение об ошибке.

Настройка через .env:
  OPENAI_API_KEY=sk-proj-...
  OPENROUTER_API_KEY=sk-or-v1-...
  YANDEX_API_KEY=AQVN...
  YANDEX_FOLDER_ID=b1g...
  GIGACHAT_CREDENTIALS=...

  # Формат: "provider:model_name"
  AI_TIER1=openrouter:google/gemini-3.7-flash-low
  AI_TIER2=openrouter:anthropic/claude-sonnet-4-6
  AI_TIER3=openrouter:anthropic/claude-haiku-4-5
"""

import os
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_tier(spec: str) -> Tuple[str, str]:
    """'openrouter:google/gemini-3.7-flash-low' → ('openrouter', 'google/gemini-3.7-flash-low')."""
    if ":" in spec:
        provider, model = spec.split(":", 1)
        return provider.strip().lower(), model.strip()
    # Без двоеточия — провайдер openai с дефолтной моделью
    return spec.strip().lower(), ""


def _get_tier(spec: str):
    """Возвращает кортеж (callable, model_name) или None.
    callable(prompt, system, max_tokens) → str
    """
    provider, model = _parse_tier(spec)

    if provider == "openai":
        try:
            from openai import AsyncOpenAI
            key = os.getenv("OPENAI_API_KEY")
            if not key:
                return None
            client = AsyncOpenAI(api_key=key)
            return (client, model or "gpt-4o-mini")
        except ImportError:
            logger.warning("openai не установлен: pip install openai")
            return None

    if provider == "openrouter":
        try:
            from openai import AsyncOpenAI
            key = os.getenv("OPENROUTER_API_KEY")
            if not key:
                return None
            # OpenRouter совместим с OpenAI SDK — это просто другой base_url
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://openrouter.ai/api/v1",
            )
            return (client, model or "google/gemini-3.7-flash-low")
        except ImportError:
            logger.warning("openai не установлен: pip install openai (для OpenRouter)")
            return None

    if provider == "clodex":
        try:
            from openai import AsyncOpenAI
            key = os.getenv("CLODEX_API_KEY") or os.getenv("OPENROUTER_API_KEY")
            if not key:
                return None
            # clodex.xyz — OpenAI-compatible endpoint https://clodex.xyz/v1
            client = AsyncOpenAI(
                api_key=key,
                base_url="https://clodex.xyz/v1",
            )
            # Модели clodex без префикса провайдера (gemini-3.7-flash-low, gpt-5.6-sol)
            return (client, model or "gemini-3.7-flash-low")
        except ImportError:
            logger.warning("openai не установлен: pip install openai (для clodex)")
            return None

    if provider == "yandex":
        try:
            import httpx
            key = os.getenv("YANDEX_API_KEY")
            folder = os.getenv("YANDEX_FOLDER_ID")
            if not (key and folder):
                return None
            return (_ask_yandex, model or "yandexgpt-lite")
        except ImportError:
            logger.warning("httpx не установлен: pip install httpx")
            return None

    if provider == "gigachat":
        key = os.getenv("GIGACHAT_CREDENTIALS")
        if not key:
            return None
        return (_ask_gigachat, model or "GigaChat-Pro")

    return None



# ─── Yandex Cloud ────────────────────────────────────────────────────────────

async def _ask_yandex(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    model: str = "yandexgpt-lite",
    temperature: float = 0.3,
    _api_key: Optional[str] = None,
    _folder_id: Optional[str] = None,
) -> str:
    """REST-клиент для YandexGPT (Foundation Models v1 API)."""
    import httpx
    api_key = _api_key or os.getenv("YANDEX_API_KEY")
    folder_id = _folder_id or os.getenv("YANDEX_FOLDER_ID")
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "modelUri": f"gpt://{folder_id}/{model}/latest",
        "completionOptions": {
            "stream": False,
            "temperature": temperature,
            "maxTokens": str(max_tokens),
        },
        "messages": [],
    }
    if system:
        body["messages"].append({"role": "system", "text": system})
    body["messages"].append({"role": "user", "text": prompt})

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            json=body,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
        return data["result"]["alternatives"][0]["message"]["text"]


# ─── GigaChat ────────────────────────────────────────────────────────────────

async def _ask_gigachat(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    model: str = "GigaChat-Pro",
    temperature: float = 0.3,
    _credentials: Optional[str] = None,
    _scope: str = "GIGACHAT_API_PERS",
) -> str:
    """Клиент GigaChat (Сбер). Требует httpx."""
    import httpx
    import time
    credentials = _credentials or os.getenv("GIGACHAT_CREDENTIALS")
    # Кэш токена
    if not hasattr(_ask_gigachat, "_token") or not hasattr(_ask_gigachat, "_expires"):
        _ask_gigachat._token = None
        _ask_gigachat._expires = 0.0

    if not _ask_gigachat._token or time.time() > _ask_gigachat._expires - 60:
        headers = {
            "Authorization": f"Basic {credentials}",
            "RqUID": "6f0b1291-c7f3-43c9-b313-1337d9298ff5",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
            r = await client.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers=headers,
                data={"scope": _scope},
            )
            r.raise_for_status()
            j = r.json()
            _ask_gigachat._token = j["access_token"]
            _ask_gigachat._expires = j["expires_at"] / 1000

    headers = {
        "Authorization": f"Bearer {_ask_gigachat._token}",
        "Content-Type": "application/json",
    }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            json=body,
            headers=headers,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# ─── OpenAI-совместимые клиенты (OpenAI, OpenRouter) ────────────────────────

async def _ask_openai_compatible(
    prompt: str,
    system: str = "",
    max_tokens: int = 1000,
    model: str = "gpt-4o-mini",
    temperature: float = 0.3,
    _client=None,
) -> str:
    """Универсальный вызов для OpenAI и OpenRouter (одинаковый API)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = await _client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    return response.choices[0].message.content


# ─── Универсальный клиент с fallback ────────────────────────────────────────

class AIClient:
    """Главный фасад. Используется из bot.py."""

    def __init__(self):
        # Формат .env: "provider:model" (например "openrouter:google/gemini-3.7-flash-low")
        self.tier1_spec = os.getenv("AI_TIER1", "openai:gpt-4o-mini")
        self.tier2_spec = os.getenv("AI_TIER2", "openai:gpt-4o")
        self.tier3_spec = os.getenv("AI_TIER3", "")
        self.tier1 = _get_tier(self.tier1_spec)
        self.tier2 = _get_tier(self.tier2_spec)
        self.tier3 = _get_tier(self.tier3_spec) if self.tier3_spec else None

    @property
    def available(self) -> bool:
        """Хотя бы один tier доступен."""
        return any(t is not None for t in (self.tier1, self.tier2, self.tier3))

    async def simple_summarize(
        self, report_text: str, marketplace: str, max_tokens: int = 800
    ) -> str:
        """
        Короткий AI-саммари по готовому отчёту (Tier 1).
        Стоит ~$0.0002 (gemini-3.7-flash-low) или ~$0.001 (gpt-4o-mini).
        """
        system = (
            "Ты финансовый аналитик для селлеров маркетплейсов. "
            "Твоя задача — дать короткий, конкретный, actionable совет "
            "по готовому финансовому отчёту. "
            "Без воды. Без вступлений. Сразу по делу. "
            "Пиши кратко: 3-5 пунктов по 1-2 предложения."
        )
        prompt = (
            f"Маркетплейс: {marketplace}\n\n"
            f"Отчёт селлера:\n```\n{report_text[:3000]}\n```\n\n"
            "Что самое важное в этом отчёте? "
            "Что нужно сделать в первую очередь? "
            "Где теряются деньги?"
        )
        return await self._ask_with_fallback(
            prompt, system=system, max_tokens=max_tokens,
        )

    async def compare_reports(
        self,
        old_report: str,
        new_report: str,
        marketplace: str,
        max_tokens: int = 1500,
    ) -> str:
        """
        AI-сравнение двух отчётов (Tier 2 → Tier 3).
        Стоит ~$0.01-0.03 за вызов.
        """
        system = (
            "Ты финансовый аналитик. Сравни два отчёта селлера "
            "(старый и новый). "
            "Найди: что улучшилось, что ухудшилось, какие товары "
            "просели в прибыли. Дай 3-5 конкретных рекомендаций."
        )
        prompt = (
            f"Маркетплейс: {marketplace}\n\n"
            f"СТАРЫЙ ОТЧЁТ:\n```\n{old_report[:4000]}\n```\n\n"
            f"НОВЫЙ ОТЧЁТ:\n```\n{new_report[:4000]}\n```\n\n"
            "Что изменилось? На что обратить внимание?"
        )
        return await self._ask_with_fallback(
            prompt, system=system, max_tokens=max_tokens,
            prefer_tier=2,
        )

    async def chat_about_report(
        self,
        report_text: str,
        question: str,
        marketplace: str,
        max_tokens: int = 1000,
    ) -> str:
        """AI-чат: пользователь задаёт вопрос по своему отчёту (Tier 1 → Tier 2)."""
        system = (
            "Ты финансовый аналитик. У тебя есть отчёт селлера. "
            "Отвечай коротко (2-5 предложений), конкретно, по цифрам "
            "из отчёта. Если в отчёте нет нужных данных — скажи это прямо."
        )
        prompt = (
            f"Маркетплейс: {marketplace}\n\n"
            f"ОТЧЁТ:\n```\n{report_text[:4000]}\n```\n\n"
            f"ВОПРОС СЕЛЛЕРА: {question}"
        )
        return await self._ask_with_fallback(
            prompt, system=system, max_tokens=max_tokens,
        )

    async def _ask_with_fallback(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 1000,
        prefer_tier: int = 1,
    ) -> str:
        """Пробует prefer_tier, при ошибке → следующие tiers."""
        if prefer_tier == 1:
            tiers = [self.tier1, self.tier2, self.tier3]
        elif prefer_tier == 2:
            tiers = [self.tier2, self.tier3, self.tier1]
        else:
            tiers = [self.tier3, self.tier1, self.tier2]

        last_error: Optional[Exception] = None
        for tier in tiers:
            if tier is None:
                continue
            client, model = tier
            try:
                if client in (_ask_yandex, _ask_gigachat):
                    result = await client(
                        prompt,
                        system=system,
                        max_tokens=max_tokens,
                        model=model,
                    )
                else:
                    # OpenAI / OpenRouter (универсальный callable)
                    result = await _ask_openai_compatible(
                        prompt,
                        system=system,
                        max_tokens=max_tokens,
                        model=model,
                        _client=client,
                    )
                logger.info(
                    "AI OK через %s (model=%s)",
                    type(client).__name__ if hasattr(client, "__name__") else "client",
                    model,
                )
                return result
            except Exception as exc:
                logger.warning("AI-провайдер упал: %s (model=%s)", exc, model)
                last_error = exc
                continue

        # Ничего не сработало
        logger.error("Все AI-провайдеры недоступны: %s", last_error)
        return (
            "🤖 AI-анализ временно недоступен.\n\n"
            "Проверьте настройки API-ключей в .env:\n"
            "  • OPENAI_API_KEY\n"
            "  • OPENROUTER_API_KEY\n"
            "  • YANDEX_API_KEY + YANDEX_FOLDER_ID\n"
            "  • GIGACHAT_CREDENTIALS"
        )


# ─── Синглтон ────────────────────────────────────────────────────────────────

_client: Optional[AIClient] = None


def get_ai_client() -> AIClient:
    """Ленивая инициализация — создаётся при первом вызове."""
    global _client
    if _client is None:
        _client = AIClient()
    return _client


async def ai_summarize_report(report_text: str, marketplace: str) -> str:
    """Удобная функция для быстрого вызова."""
    return await get_ai_client().simple_summarize(report_text, marketplace)


async def ai_compare_reports(
    old_report: str, new_report: str, marketplace: str,
) -> str:
    return await get_ai_client().compare_reports(
        old_report, new_report, marketplace,
    )


async def ai_chat(report_text: str, question: str, marketplace: str) -> str:
    return await get_ai_client().chat_about_report(
        report_text, question, marketplace,
    )

