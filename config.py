"""
PredskazBot v2.1 – unified provider config
Supports: OpenAI, Moonshot/KIMI, Venice, OpenRouter, DeepSeek, Groq, custom OpenAI-compatible proxy
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

@dataclass
class LLMProvider:
    name: str
    api_key: str
    base_url: Optional[str]
    model: str
    extra_headers: dict | None = None
    extra_body: dict | None = None

def _clean(v: str | None) -> str:
    return (v or "").strip().strip('"').strip("'")

def detect_provider() -> LLMProvider:
    # 1. Explicit OPENAI_*
    openai_key = _clean(os.getenv("OPENAI_API_KEY"))
    openai_base = _clean(os.getenv("OPENAI_BASE_URL"))
    openai_model = _clean(os.getenv("OPENAI_MODEL"))

    # 2. Venice
    venice_key = _clean(os.getenv("VENICE_API_KEY"))
    if venice_key and not openai_key:
        openai_key = venice_key
        openai_base = openai_base or "https://api.venice.ai/api/v1"
        openai_model = openai_model or "qwen3-4b"
        return LLMProvider(
            name="venice",
            api_key=openai_key,
            base_url=openai_base,
            model=openai_model,
            extra_body={"venice_parameters": {"include_venice_system_prompt": False}}
        )

    # 3. Moonshot / KIMI Code
    moonshot_key = _clean(os.getenv("MOONSHOT_API_KEY"))
    if moonshot_key and not openai_key:
        # detect which endpoint user wants
        base = openai_base or os.getenv("MOONSHOT_BASE_URL", "").strip() or "https://api.moonshot.ai/v1"
        # kimi code product uses different host
        if "kimi.com" in base or os.getenv("KIMI_CODE", "").lower() in ("1", "true", "yes"):
            base = "https://api.kimi.com/coding/v1"
            model = openai_model or "kimi-k2.7-code"
        else:
            model = openai_model or "moonshot-v1-8k"
        return LLMProvider("moonshot", moonshot_key, base, model)

    # 4. OpenRouter
    openrouter_key = _clean(os.getenv("OPENROUTER_API_KEY"))
    if openrouter_key and not openai_key:
        return LLMProvider(
            name="openrouter",
            api_key=openrouter_key,
            base_url=openai_base or "https://openrouter.ai/api/v1",
            model=openai_model or "openai/gpt-4o-mini",
            extra_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/AdamFolz/BOTOVODYVROT-main"),
                "X-Title": os.getenv("OPENROUTER_TITLE", "PredskazBot"),
            }
        )

    # 5. DeepSeek
    deepseek_key = _clean(os.getenv("DEEPSEEK_API_KEY"))
    if deepseek_key and not openai_key:
        return LLMProvider("deepseek", deepseek_key, openai_base or "https://api.deepseek.com/v1", openai_model or "deepseek-chat")

    # 6. Groq
    groq_key = _clean(os.getenv("GROQ_API_KEY"))
    if groq_key and not openai_key:
        return LLMProvider("groq", groq_key, openai_base or "https://api.groq.com/openai/v1", openai_model or "llama-3.1-8b-instant")

    # 7. Generic OpenAI-compatible (proxy like vip.j3gb.com, bizdecipher, etc)
    if openai_key:
        # auto-fix common proxy mistakes
        if openai_base and not openai_base.endswith("/v1") and "openai.com" not in openai_base:
            # many proxies give bare domain, try append /v1
            if openai_base.count("/") == 2:  # https://host
                openai_base = openai_base.rstrip("/") + "/v1"
        provider_name = "custom"
        if "venice" in openai_base: provider_name = "venice"
        elif "amvera" in openai_base: provider_name = "amvera"
        elif "moonshot" in openai_base: provider_name = "moonshot"
        elif "kimi.com" in openai_base: provider_name = "kimi-code"
        elif "openrouter" in openai_base: provider_name = "openrouter"
        elif "deepseek" in openai_base: provider_name = "deepseek"
        elif "groq" in openai_base: provider_name = "groq"
        elif "j3gb" in openai_base or "bizdecipher" in openai_base or "vip." in openai_base: provider_name = "proxy"
        else: provider_name = "openai" if not openai_base or "openai.com" in openai_base else "custom"
        model = openai_model or "gpt-4o-mini"
        extra_body = None
        if provider_name == "venice":
            extra_body = {"venice_parameters": {"include_venice_system_prompt": False}}
        return LLMProvider(provider_name, openai_key, openai_base or None, model, extra_body=extra_body)

    # No key found
    return LLMProvider("none", "", None, openai_model or "gpt-4o-mini")


# --- Telegram / app settings ---
TELEGRAM_BOT_TOKEN = _clean(os.getenv("TELEGRAM_BOT_TOKEN"))
DATABASE_PATH = _clean(os.getenv("DATABASE_PATH")) or "predskazbot.sqlite3"
ADMIN_USER_ID = int(_clean(os.getenv("ADMIN_USER_ID")) or "0")
ALLOWED_CHAT_IDS = {
    int(x.strip()) for x in _clean(os.getenv("ALLOWED_CHAT_IDS")).split(",")
    if x.strip().lstrip("-").isdigit()
}
MAX_RECENT_MESSAGES = int(_clean(os.getenv("MAX_RECENT_MESSAGES")) or "80")
MAX_RECENT_BOT_RESPONSES = int(_clean(os.getenv("MAX_RECENT_BOT_RESPONSES")) or "80")
REGENERATION_ATTEMPTS = int(_clean(os.getenv("REGENERATION_ATTEMPTS")) or "3")
FUTURE_COOLDOWN_SECONDS = int(_clean(os.getenv("FUTURE_COOLDOWN_SECONDS")) or "20")
SUMMARY_COOLDOWN_SECONDS = int(_clean(os.getenv("SUMMARY_COOLDOWN_SECONDS")) or "60")
ASK_COOLDOWN_SECONDS = int(_clean(os.getenv("ASK_COOLDOWN_SECONDS")) or "15")

KNOWLEDGE_BASE_DIR = _clean(os.getenv("KNOWLEDGE_BASE_DIR")) or "AI_Knowledge_Base"
KB_SEARCH_LIMIT = int(_clean(os.getenv("KB_SEARCH_LIMIT")) or "5")

# V2
V2_MEMORY_ENABLED = _clean(os.getenv("V2_MEMORY_ENABLED", "1")) not in ("0", "false", "False")
V2_FULL_TRANSITION = _clean(os.getenv("V2_FULL_TRANSITION", "0")) in ("1", "true", "True")
V1_MEMORY_FALLBACK_ENABLED = _clean(os.getenv("V1_MEMORY_FALLBACK_ENABLED", "1")) not in ("0", "false", "False")
V2_SQLITE_PATH = _clean(os.getenv("V2_SQLITE_PATH")) or "predskazbot_v2.sqlite3"

LLM = detect_provider()

def validate() -> list[str]:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not LLM.api_key:
        missing.append("OPENAI_API_KEY (или VENICE_API_KEY / MOONSHOT_API_KEY / OPENROUTER_API_KEY / GROQ_API_KEY / DEEPSEEK_API_KEY)")
    return missing

def provider_info() -> str:
    base = LLM.base_url or "https://api.openai.com/v1"
    key_masked = (LLM.api_key[:7] + "…" + LLM.api_key[-4:]) if len(LLM.api_key) > 12 else "set" if LLM.api_key else "missing"
    return f"{LLM.name} | model={LLM.model} | base={base} | key={key_masked}"
