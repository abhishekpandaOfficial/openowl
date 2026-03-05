"""
OpenOwl Model Router
OSS-first: Groq (free) → Ollama (local) → Together.ai → Claude Haiku → GPT-4o-mini
Automatically tries cheaper/free options before spending money.
"""
import time
import logging
from typing import Optional
from enum import Enum

from config import settings

logger = logging.getLogger(__name__)


class ModelProvider(str, Enum):
    GROQ        = "groq"
    OLLAMA      = "ollama"
    TOGETHER    = "together"
    ANTHROPIC   = "anthropic"
    OPENAI      = "openai"


# ── Task complexity → model size recommendation ─────────────────────────────
TASK_MODEL_MAP = {
    "conversation":  "small",     # Phi/Mistral-7B sufficient
    "reminder":      "small",
    "search":        "small",
    "learn":         "medium",    # Mistral-7B good for explanation
    "calendar":      "small",
    "booking":       "medium",
    "research":      "large",     # LLaMA 70B for deep research
    "email":         "medium",
    "payment":       "large",     # Use best model for money tasks
    "code":          "large",
    "unknown":       "medium",
}


class ModelRouter:
    """
    Routes each request to the best available model.
    Tries free/OSS models first, falls back to paid only if needed.
    """

    def __init__(self):
        self._groq_client = None
        self._openai_client = None
        self._anthropic_client = None
        self._groq_request_count = 0

    # ── Lazy client initialization ───────────────────────────────────────────

    def _get_groq(self):
        if not self._groq_client and settings.groq_api_key:
            try:
                from groq import Groq
                self._groq_client = Groq(api_key=settings.groq_api_key)
            except ImportError:
                logger.warning("groq package not installed. Run: pip install groq")
        return self._groq_client

    def _get_anthropic(self):
        if not self._anthropic_client and settings.anthropic_api_key:
            try:
                import anthropic
                self._anthropic_client = anthropic.Anthropic(
                    api_key=settings.anthropic_api_key
                )
            except ImportError:
                logger.warning("anthropic package not installed.")
        return self._anthropic_client

    def _get_openai(self):
        if not self._openai_client and settings.openai_api_key:
            try:
                from openai import OpenAI
                self._openai_client = OpenAI(api_key=settings.openai_api_key)
            except ImportError:
                logger.warning("openai package not installed.")
        return self._openai_client

    # ── Model selection logic ────────────────────────────────────────────────

    def _select_groq_model(self, size: str) -> str:
        """Pick the right Groq model based on task complexity."""
        if size == "small":
            return settings.groq_small_model      # llama-3.1-8b-instant
        elif size == "large":
            return settings.groq_large_model      # llama-3.3-70b-versatile
        else:
            return settings.groq_primary_model    # mistral-saba-24b

    # ── Provider call methods ────────────────────────────────────────────────

    async def _call_groq(
        self,
        messages: list[dict],
        task_type: str = "conversation",
        system_prompt: str = "",
    ) -> tuple[str, str, int]:
        """Returns (response_text, model_name, latency_ms)"""
        client = self._get_groq()
        if not client:
            raise ValueError("Groq not configured")

        size = TASK_MODEL_MAP.get(task_type, "medium")
        model = self._select_groq_model(size)

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        start = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=full_messages,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
        )
        latency = int((time.time() - start) * 1000)
        text = response.choices[0].message.content
        self._groq_request_count += 1
        return text, f"groq/{model}", latency

    async def _call_ollama(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> tuple[str, str, int]:
        """Call local Ollama. Returns (response_text, model_name, latency_ms)"""
        try:
            import httpx
            full_messages = []
            if system_prompt:
                full_messages.append({"role": "system", "content": system_prompt})
            full_messages.extend(messages)

            start = time.time()
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{settings.ollama_base_url}/api/chat",
                    json={
                        "model": settings.ollama_model,
                        "messages": full_messages,
                        "stream": False,
                        "options": {
                            "temperature": settings.temperature,
                            "num_predict": settings.max_tokens,
                        },
                    },
                )
                response.raise_for_status()
                data = response.json()
            latency = int((time.time() - start) * 1000)
            return data["message"]["content"], f"ollama/{settings.ollama_model}", latency
        except Exception as e:
            raise ValueError(f"Ollama failed: {e}")

    async def _call_anthropic(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> tuple[str, str, int]:
        """Call Claude Haiku as first cloud fallback."""
        client = self._get_anthropic()
        if not client:
            raise ValueError("Anthropic not configured")

        # Anthropic uses separate system param
        start = time.time()
        response = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.max_tokens,
            system=system_prompt or "You are OpenOwl, a helpful personal assistant.",
            messages=messages,
        )
        latency = int((time.time() - start) * 1000)
        return response.content[0].text, f"anthropic/{settings.claude_model}", latency

    async def _call_openai(
        self,
        messages: list[dict],
        system_prompt: str = "",
    ) -> tuple[str, str, int]:
        """Call GPT-4o-mini as last resort fallback."""
        client = self._get_openai()
        if not client:
            raise ValueError("OpenAI not configured")

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        start = time.time()
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=full_messages,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
        )
        latency = int((time.time() - start) * 1000)
        return response.choices[0].message.content, f"openai/{settings.openai_model}", latency

    # ── Main routing method ──────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[dict],
        task_type: str = "conversation",
        system_prompt: str = "",
        force_provider: Optional[str] = None,
    ) -> tuple[str, str, int]:
        """
        Route to the best available model with automatic fallback.

        Returns: (response_text, model_used, latency_ms)

        Priority chain:
          1. Groq (free OSS — Mistral/LLaMA)
          2. Ollama (local, private, free)
          3. Claude Haiku (cheapest cloud)
          4. GPT-4o-mini (last resort)
        """

        # Override chain if forced
        if force_provider:
            return await self._try_provider(force_provider, messages, task_type, system_prompt)

        # Priority 1: Groq (fastest free option)
        if settings.groq_api_key:
            try:
                logger.info("🟢 Trying Groq (OSS, free)...")
                result = await self._call_groq(messages, task_type, system_prompt)
                logger.info(f"✅ Groq responded in {result[2]}ms using {result[1]}")
                return result
            except Exception as e:
                logger.warning(f"⚠️  Groq failed: {e} — trying next")

        # Priority 2: Ollama (local)
        if settings.ollama_enabled:
            try:
                logger.info("🟡 Trying Ollama (local)...")
                result = await self._call_ollama(messages, system_prompt)
                logger.info(f"✅ Ollama responded in {result[2]}ms")
                return result
            except Exception as e:
                logger.warning(f"⚠️  Ollama failed: {e} — trying cloud fallback")

        # Fallback 1: Claude Haiku
        if settings.anthropic_api_key:
            try:
                logger.info("🔶 Falling back to Claude Haiku...")
                result = await self._call_anthropic(messages, system_prompt)
                logger.info(f"✅ Claude responded in {result[2]}ms")
                return result
            except Exception as e:
                logger.warning(f"⚠️  Claude failed: {e} — trying OpenAI")

        # Fallback 2: GPT-4o-mini
        if settings.openai_api_key:
            try:
                logger.info("🔴 Last resort: GPT-4o-mini...")
                result = await self._call_openai(messages, system_prompt)
                logger.info(f"✅ OpenAI responded in {result[2]}ms")
                return result
            except Exception as e:
                logger.error(f"❌ All models failed. Last error: {e}")

        raise RuntimeError(
            "All AI models are unavailable. "
            "Please check your API keys in .env file."
        )

    async def _try_provider(self, provider: str, messages, task_type, system_prompt):
        if provider == "groq":
            return await self._call_groq(messages, task_type, system_prompt)
        elif provider == "ollama":
            return await self._call_ollama(messages, system_prompt)
        elif provider == "anthropic":
            return await self._call_anthropic(messages, system_prompt)
        elif provider == "openai":
            return await self._call_openai(messages, system_prompt)
        raise ValueError(f"Unknown provider: {provider}")


# Singleton
model_router = ModelRouter()
