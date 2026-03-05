"""
OpenOwl Configuration
Loads all settings from environment variables.
"""
from pydantic_settings import BaseSettings
from typing import Optional
from functools import lru_cache


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────
    app_name: str = "OpenOwl"
    app_version: str = "1.0.0"
    debug: bool = False
    secret_key: str = "change-me-in-production-use-openssl-rand-hex-32"
    owner_id: str = ""           # Your Telegram user ID (get from @userinfobot)

    # ── Database ─────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://openowl:openowl@localhost:5432/openowl"
    redis_url: str = "redis://localhost:6379/0"

    # ── Telegram ─────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_webhook_url: str = ""     # e.g. https://your-ngrok-url.ngrok.io

    # ── Twilio (WhatsApp + SMS + Calls) ──────────────────
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""      # e.g. +14155551234
    twilio_whatsapp_number: str = ""   # e.g. whatsapp:+14155551234

    # ── AI Models — OSS First ─────────────────────────────
    # Priority 1: Groq (free, 6000 req/day, 300 tok/sec)
    groq_api_key: str = ""
    groq_primary_model: str = "mistral-saba-24b"
    groq_large_model: str = "llama-3.3-70b-versatile"
    groq_small_model: str = "llama-3.1-8b-instant"

    # Priority 2: Ollama (local, free, private)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mistral:7b"
    ollama_enabled: bool = True

    # Priority 3: Together.ai (free $25 credit)
    together_api_key: str = ""
    together_model: str = "mistralai/Mistral-7B-Instruct-v0.2"

    # Fallback 1: Claude (cheapest = Haiku)
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-4-5-20251001"  # cheapest Anthropic model

    # Fallback 2: OpenAI (last resort)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # ── Agent Settings ────────────────────────────────────
    default_persona: str = "aria"
    max_tokens: int = 1024
    temperature: float = 0.7
    context_window: int = 10       # last N messages to keep in context

    # ── Guardrails ────────────────────────────────────────
    require_approval_for_payments: bool = True
    require_approval_for_emails: bool = True
    require_approval_for_messages: bool = True
    approval_timeout_seconds: int = 300   # 5 minutes to respond

    # ── Integrations ─────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # ── Webhooks ─────────────────────────────────────────
    webhook_secret: str = "change-me"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
