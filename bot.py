import asyncio
import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI, AuthenticationError
from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from database import Database
from knowledge_base import KnowledgeBase
from memory import MemoryManager
from prompts import ASK_PROMPT, CORE_STYLE_SYSTEM, FUTURE_PROMPT, KB_ANSWER_PROMPT, SUMMARY_PROMPT, PARTICIPANT_PROMPT
from utils import clean_bot_reply, extract_mentions, is_too_similar, safe_format, safe_short


load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("predskazbot")

# Persist /set_style choice under Amvera /data (ephemeral FS otherwise).
_STYLE_STATE_PATH = Path("/data/style_choice.txt") if Path("/data").exists() else Path("style_choice.txt")
_STYLE_MODULES = {
    "unrestricted": "prompts_v3_unrestricted",
    "toxic3": "prompts_v3_unrestricted",
    "smart": "prompts_smart",
    "bro": "prompts_smart",
    "toxic": "prompts_v2_toxic",
    "toxic2": "prompts_v2_toxic",
    "clean": "prompts_v2_2",
    "v2": "prompts_v2_2",
}


def _apply_style_module(module_name: str) -> None:
    """Hot-reload a prompt preset module into process globals (no file copy)."""
    global CORE_STYLE_SYSTEM, FUTURE_PROMPT, SUMMARY_PROMPT, ASK_PROMPT, KB_ANSWER_PROMPT, PARTICIPANT_PROMPT
    import importlib

    preset = importlib.import_module(module_name)
    importlib.reload(preset)
    CORE_STYLE_SYSTEM = preset.CORE_STYLE_SYSTEM
    FUTURE_PROMPT = preset.FUTURE_PROMPT
    SUMMARY_PROMPT = preset.SUMMARY_PROMPT
    ASK_PROMPT = preset.ASK_PROMPT
    KB_ANSWER_PROMPT = preset.KB_ANSWER_PROMPT
    PARTICIPANT_PROMPT = preset.PARTICIPANT_PROMPT


try:
    if _STYLE_STATE_PATH.exists():
        _saved = _STYLE_STATE_PATH.read_text(encoding="utf-8").strip()
        if _saved:
            _apply_style_module(_saved)
except Exception:
    pass  # never block startup on style restore

# --- Unified config v2.1 ---
try:
    import config as app_config
    TELEGRAM_BOT_TOKEN = app_config.TELEGRAM_BOT_TOKEN
    DATABASE_PATH = app_config.DATABASE_PATH
    ADMIN_USER_ID = app_config.ADMIN_USER_ID
    ALLOWED_CHAT_IDS = app_config.ALLOWED_CHAT_IDS
    MAX_RECENT_MESSAGES = app_config.MAX_RECENT_MESSAGES
    MAX_RECENT_BOT_RESPONSES = app_config.MAX_RECENT_BOT_RESPONSES
    REGENERATION_ATTEMPTS = app_config.REGENERATION_ATTEMPTS
    FUTURE_COOLDOWN_SECONDS = app_config.FUTURE_COOLDOWN_SECONDS
    SUMMARY_COOLDOWN_SECONDS = app_config.SUMMARY_COOLDOWN_SECONDS
    ASK_COOLDOWN_SECONDS = app_config.ASK_COOLDOWN_SECONDS
    KNOWLEDGE_BASE_DIR = app_config.KNOWLEDGE_BASE_DIR
    KB_SEARCH_LIMIT = app_config.KB_SEARCH_LIMIT

    OPENAI_API_KEY = app_config.LLM.api_key
    OPENAI_MODEL = app_config.LLM.model
    OPENAI_BASE_URL = app_config.LLM.base_url or ""
    LLM_PROVIDER = app_config.LLM.name
    LLM_EXTRA_HEADERS = app_config.LLM.extra_headers or {}
    LLM_EXTRA_BODY = app_config.LLM.extra_body or {}

    logger.info("LLM provider: %s", app_config.provider_info())
except Exception as e:
    # fallback to legacy env parsing so old tests still work
    logger.warning("config.py load failed, falling back to legacy env: %s", e)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    MOONSHOT_API_KEY = os.getenv("MOONSHOT_API_KEY", "").strip()
    VENICE_API_KEY = os.getenv("VENICE_API_KEY", "").strip()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or MOONSHOT_API_KEY or VENICE_API_KEY
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "").strip() or ("gpt-4o-mini")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
    # auto-detect venice
    if VENICE_API_KEY and not os.getenv("OPENAI_BASE_URL"):
        OPENAI_BASE_URL = "https://api.venice.ai/api/v1"
        if not os.getenv("OPENAI_MODEL"):
            OPENAI_MODEL = "qwen3-4b"
    # auto-fix proxy base_url missing /v1
    if OPENAI_BASE_URL and "openai.com" not in OPENAI_BASE_URL and not OPENAI_BASE_URL.endswith("/v1"):
        if OPENAI_BASE_URL.count("/") == 2:
            OPENAI_BASE_URL = OPENAI_BASE_URL.rstrip("/") + "/v1"
    DATABASE_PATH = os.getenv("DATABASE_PATH", "predskazbot.sqlite3").strip()
    MAX_RECENT_MESSAGES = int(os.getenv("MAX_RECENT_MESSAGES", "80"))
    MAX_RECENT_BOT_RESPONSES = int(os.getenv("MAX_RECENT_BOT_RESPONSES", "80"))
    REGENERATION_ATTEMPTS = int(os.getenv("REGENERATION_ATTEMPTS", "3"))
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "1019731503") or "1019731503")
    FUTURE_COOLDOWN_SECONDS = int(os.getenv("FUTURE_COOLDOWN_SECONDS", "20"))
    SUMMARY_COOLDOWN_SECONDS = int(os.getenv("SUMMARY_COOLDOWN_SECONDS", "60"))
    KNOWLEDGE_BASE_DIR = os.getenv("KNOWLEDGE_BASE_DIR", "AI_Knowledge_Base").strip()
    KB_SEARCH_LIMIT = int(os.getenv("KB_SEARCH_LIMIT", "5"))
    ASK_COOLDOWN_SECONDS = int(os.getenv("ASK_COOLDOWN_SECONDS", "15"))
    ALLOWED_CHAT_IDS = {
        int(item.strip())
        for item in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
        if item.strip().lstrip("-").isdigit()
    }
    if ADMIN_USER_ID > 0 and ALLOWED_CHAT_IDS:
        ALLOWED_CHAT_IDS.add(ADMIN_USER_ID)
    LLM_PROVIDER = "legacy"
    LLM_EXTRA_HEADERS = {}
    LLM_EXTRA_BODY = {"venice_parameters": {"include_venice_system_prompt": False}} if "venice.ai" in OPENAI_BASE_URL else {}

db = Database(DATABASE_PATH)
# Build OpenAI client with optional extra headers
openai_client = AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    base_url=OPENAI_BASE_URL or None,
    default_headers=LLM_EXTRA_HEADERS if 'LLM_EXTRA_HEADERS' in globals() else None,
)
memory_manager = MemoryManager(db, openai_client, OPENAI_MODEL)
knowledge_base = KnowledgeBase(KNOWLEDGE_BASE_DIR)

# helper to inject provider-specific extra_body
def _llm_kwargs(extra: dict | None = None) -> dict:
    base = {}
    if 'LLM_EXTRA_BODY' in globals() and LLM_EXTRA_BODY:
        base.update(LLM_EXTRA_BODY)
    if extra:
        base.update(extra)
    return {"extra_body": base} if base else {}

future_rate_limit: dict[tuple[int, int], float] = defaultdict(float)
ask_rate_limit: dict[tuple[int, int], float] = defaultdict(float)
summary_rate_limit: dict[int, float] = defaultdict(float)
participant_rate_limit: dict[tuple[int, int], float] = defaultdict(float)
spontaneous_rate_limit: dict[int, float] = defaultdict(float)


def sanitize_display_name(name: str) -> str:
    if not name:
        return "Участник"
    name = name.replace("SEKSYALKA_PREDSKAZALKA_BOT", "Участник").replace("[ТЫ]", "").replace("[bot]", "").replace("Хуебот", "Участник")
    name = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9_ -]", "", name).strip()
    return name[:32] or "Участник"


def user_display_name(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Неизвестный"
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    raw = name or user.username or str(user.id)
    return sanitize_display_name(raw)


def username_of(update: Update) -> str:
    user = update.effective_user
    if not user:
        return ""
    return user.username or ""


def chat_id_of(update: Update) -> int:
    chat = update.effective_chat
    if not chat:
        raise RuntimeError("No chat in update")
    return int(chat.id)


def user_id_of(update: Update) -> int:
    user = update.effective_user
    if not user:
        raise RuntimeError("No user in update")
    return int(user.id)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and ADMIN_USER_ID and user.id == ADMIN_USER_ID)


def admin_denied_message() -> str:
    if ADMIN_USER_ID <= 0:
        return (
            "Эта команда доступна только админу, а ADMIN_USER_ID не настроен. "
            "Задай его в .env, чтобы включить админ-команды."
        )
    return "Эта команда доступна только админу."


def is_allowed_chat_id(chat_id: int) -> bool:
    """Allow empty allowlist (all chats) or explicit chat ids.

    Admin private DM (chat_id == ADMIN_USER_ID in Telegram) is always allowed
    so the owner can /health and debug without adding themselves to ALLOWED_CHAT_IDS.
    """
    cid = int(chat_id)
    if not ALLOWED_CHAT_IDS:
        return True
    if cid in ALLOWED_CHAT_IDS:
        return True
    if ADMIN_USER_ID and cid == ADMIN_USER_ID:
        return True
    return False


def record_audit(
    chat_id: int,
    actor_user_id: int,
    action: str,
    target_user_id: int | None = None,
    details: dict[str, object] | None = None,
) -> None:
    try:
        db.add_audit_log(chat_id, actor_user_id, action, target_user_id=target_user_id, details=details)
    except Exception:
        logger.exception("Failed to write audit log action=%s chat_id=%s", action, chat_id)


async def ensure_allowed_chat(update: Update) -> bool:
    try:
        chat_id = chat_id_of(update)
    except RuntimeError:
        return False
    if is_allowed_chat_id(chat_id):
        return True

    # Admin private chats always pass (defense in depth if chat_id != user_id edge cases)
    chat = update.effective_chat
    user = update.effective_user
    if (
        ADMIN_USER_ID
        and user
        and user.id == ADMIN_USER_ID
        and chat
        and chat.type == ChatType.PRIVATE
    ):
        return True

    logger.warning("Rejected update from non-allowlisted chat_id=%s", chat_id)
    await safe_send(update, "Этот чат не подключён к боту.")
    return False


def extract_first_url(text: str) -> str:
    match = re.search(r"https?://\S+", text)
    return match.group(0) if match else ""


def build_kb_query(question: str, fallback_context: str = "") -> str:
    combined = (question + "\n" + fallback_context).strip()
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9_-]{4,}", combined)
    return " ".join(words[:20]) or question.strip()


def check_user_cooldown(
    bucket: dict[tuple[int, int], float],
    chat_id: int,
    user_id: int,
    cooldown_seconds: int,
) -> int:
    now = time.time()
    key = (chat_id, user_id)
    allowed_at = bucket.get(key, 0.0)
    if now < allowed_at:
        return int(allowed_at - now) + 1
    bucket[key] = now + cooldown_seconds
    return 0


def check_chat_cooldown(
    bucket: dict[int, float],
    chat_id: int,
    cooldown_seconds: int,
) -> int:
    now = time.time()
    allowed_at = bucket.get(chat_id, 0.0)
    if now < allowed_at:
        return int(allowed_at - now) + 1
    bucket[chat_id] = now + cooldown_seconds
    return 0


async def safe_send(update: Update, text: str, max_len: int = 3500) -> None:
    chat = update.effective_chat
    if not chat:
        logger.warning("safe_send skipped: no effective chat")
        return

    payload = safe_short(text, max_len)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            await chat.send_message(payload)
            return
        except RetryAfter as exc:
            last_err = exc
            logger.warning(
                "Telegram rate limit: retry_after=%s (attempt %s/3)",
                exc.retry_after,
                attempt,
            )
            await asyncio.sleep(float(exc.retry_after) + 0.5)
        except (TimedOut, NetworkError) as exc:
            last_err = exc
            wait = min(2 ** attempt, 12)
            logger.warning(
                "Telegram network/timeout on send (attempt %s/3), sleep %ss: %s",
                attempt,
                wait,
                exc,
            )
            await asyncio.sleep(wait)
        except (BadRequest, Forbidden) as exc:
            logger.exception("Failed to send Telegram message (non-retryable): %s", exc)
            return
        except Exception as exc:
            last_err = exc
            logger.exception("Unexpected Telegram send failure")
            return
    logger.error("safe_send gave up after retries: %s", last_err)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed_chat(update):
        return
    text = (
        "Я PredskazBot. Я запоминаю конфу, строю досье и выдаю предсказания.\n\n"
        "Команды:\n"
        "/future — предсказание\n"
        "/profile — твоё досье\n"
        "/profile @username — досье участника\n"
        "/lore — лор конфы\n"
        "/remember текст — сохранить мем/факт (только админ)\n"
        "/summary — летопись последних событий\n"
        "/ask вопрос — ответ по чату и базе знаний\n"
        "/kbstatus — статус локальной базы знаний\n"
        "/kbsearch запрос — поиск по локальной базе\n"
        "/kbask вопрос — ответ только по локальной базе\n"
        "/kbimport — импортировать reply/document/url в базу знаний (админ)\n"
        "/health — статус runtime\n"
        "/privacy — что хранится и как удалить данные\n"
        "/export_me — выгрузка твоих данных\n"
        "/delete_me CONFIRM — удалить твои v1-данные\n"
        "/forget <id> — удалить ручную память (админ)\n"
        "/whoami — показать user_id/chat_id/admin debug\n"
        "/v2status — проверить, пишет ли v2 storage (только админ)"
    )
    await safe_send(update, text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        await safe_send(update, "Не вижу chat/user в update.")
        return

    target_id = await resolve_target_chat_id(update)
    text = (
        "Диагностика:\n"
        f"user_id: {user.id}\n"
        f"username: @{user.username or ''}\n"
        f"chat_id: {chat.id}\n"
        f"resolved_chat_id: {target_id}\n"
        f"chat_type: {chat.type}\n"
        f"ADMIN_USER_ID: {ADMIN_USER_ID}\n"
        f"is_admin: {is_admin(update)}\n"
        f"chat_allowed: {is_allowed_chat_id(int(chat.id))}"
    )
    await safe_send(update, text)


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    chat_id = await resolve_target_chat_id(update)
    checks: list[str] = []
    checks.append(f"bot: ok")
    provider = globals().get("LLM_PROVIDER", "unknown")
    checks.append(f"llm_provider: {provider}")
    checks.append(f"llm_model: {OPENAI_MODEL}")
    checks.append(f"llm_base: {OPENAI_BASE_URL or 'api.openai.com'}")
    checks.append(f"openai_key: {'set' if OPENAI_API_KEY else 'missing'}")
    checks.append(f"admin_user_id: {'set' if ADMIN_USER_ID else 'missing'}")
    checks.append(f"chat_allowed: {is_allowed_chat_id(chat_id)}")
    try:
        db.init()
        checks.append(f"v1_sqlite: ok ({DATABASE_PATH})")
        checks.append(f"v1_messages: {db.count_messages(chat_id)}")
        checks.append(f"v1_users: {db.count_users(chat_id)}")
        checks.append(f"manual_memories: {db.count_manual_memories(chat_id)}")
    except Exception as exc:
        checks.append(f"v1_sqlite: error ({exc})")
    try:
        checks.append(memory_manager.v2_status_text(chat_id))
    except Exception as exc:
        checks.append(f"v2: error ({exc})")
    if knowledge_base.exists():
        status = knowledge_base.status()
        checks.append(
            "kb: ok "
            f"sources={status['sources']} extracts={status['extracts']} "
            f"summaries={status['summaries']} failed={status['failed_imports']}"
        )
    else:
        checks.append(f"kb: missing ({knowledge_base.base_dir})")
    await safe_send(update, "\n".join(checks))


async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    text = (
        "Privacy:\n"
        "- Бот хранит сообщения чата, Telegram user_id, username/display name, ручную память, ответы бота и V2-события.\n"
        "- Эти данные нужны для /future, /profile, /lore, /summary, /ask и V2-памяти.\n"
        "- /export_me выгружает твои v1-данные из текущего чата.\n"
        "- /delete_me CONFIRM удаляет твои v1-сообщения, профиль, отношения, ручную память и ответы из текущего чата.\n"
        "- V2 raw event log считается append-only evidence log; его полная retention/erase policy запланирована в ROADMAP.md.\n"
        "- Админ может удалить ручную память командой /forget <id>."
    )
    await safe_send(update, text)


async def export_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    chat_id = chat_id_of(update)
    user_id = user_id_of(update)
    payload = db.export_user_data(chat_id, user_id)
    payload_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"predskazbot-export-{chat_id}-{user_id}.json"
    try:
        await context.bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(payload_bytes),
            filename=filename,
            caption="Твой v1-экспорт данных из этого чата.",
        )
    except Exception:
        logger.exception("Failed to send export document")
        await safe_send(update, safe_short(payload_bytes.decode("utf-8", errors="ignore"), 3000))


async def delete_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    confirmation = " ".join(context.args).strip() if context.args else ""
    if confirmation != "CONFIRM":
        await safe_send(update, "Для удаления v1-данных напиши: /delete_me CONFIRM")
        return
    chat_id = chat_id_of(update)
    user_id = user_id_of(update)
    counts = db.delete_user_data(chat_id, user_id)
    record_audit(chat_id, user_id, "delete_me", target_user_id=user_id, details=counts)
    deleted = ", ".join(f"{table}={count}" for table, count in counts.items())
    await safe_send(update, f"v1-данные удалены: {deleted}")


async def forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    if not is_admin(update):
        await safe_send(update, admin_denied_message())
        return
    if not context.args or not context.args[0].isdigit():
        await safe_send(update, "Напиши так: /forget <manual_memory_id>")
        return
    chat_id = chat_id_of(update)
    actor_user_id = user_id_of(update)
    memory_id = int(context.args[0])
    deleted = db.forget_manual_memory(chat_id, memory_id)
    record_audit(
        chat_id,
        actor_user_id,
        "forget_manual_memory",
        details={"manual_memory_id": memory_id, "deleted": deleted},
    )
    await safe_send(update, "Память удалена." if deleted else "Такой ручной памяти в этом чате нет.")


async def v2status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    if not is_admin(update):
        await safe_send(update, admin_denied_message())
        return
    await safe_send(update, memory_manager.v2_status_text(chat_id_of(update)))


async def kbstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed_chat(update):
        return
    if not knowledge_base.exists():
        await safe_send(update, f"База знаний не найдена по пути: {knowledge_base.base_dir}")
        return
    status = knowledge_base.status()
    recent = knowledge_base.top_summaries(limit=3)
    lines = [
        f"KB path: {knowledge_base.base_dir}",
        f"sources: {status['sources']}",
        f"extracts: {status['extracts']}",
        f"summaries: {status['summaries']}",
        f"wiki: {status['wiki']}",
        f"manifests: {status['manifests']}",
        f"failed_imports: {status['failed_imports']}",
        f"last_import_time: {status['last_import_time'] or 'none'}",
    ]
    if recent:
        lines.append("recent summaries:")
        lines.extend(f"- {item}" for item in recent)
    await safe_send(update, "\n".join(lines))


async def kbsearch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed_chat(update):
        return
    if not knowledge_base.exists():
        await safe_send(update, f"База знаний не найдена по пути: {knowledge_base.base_dir}")
        return
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await safe_send(update, "Напиши так: /kbsearch твой запрос")
        return
    hits = knowledge_base.search(query, limit=KB_SEARCH_LIMIT)
    if not hits:
        await safe_send(update, "В локальной базе ничего не найдено.")
        return
    lines = [f"Найдено по запросу: {query}"]
    for hit in hits:
        lines.append(f"- {hit.path}:{hit.line_number} — {safe_short(hit.line, 180)}")
    await safe_send(update, "\n".join(lines))


async def kbask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_allowed_chat(update):
        return
    if not knowledge_base.exists():
        await safe_send(update, f"База знаний не найдена по пути: {knowledge_base.base_dir}")
        return
    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await safe_send(update, "Напиши так: /kbask твой вопрос")
        return

    kb_context = knowledge_base.build_context(question, limit=KB_SEARCH_LIMIT, max_chars=5000)
    if not kb_context:
        await safe_send(update, "В локальной базе нет подтверждения по этому вопросу.")
        return

    try:
        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.2,
            **_llm_kwargs(),
                messages=[
                {"role": "system", "content": "Ты отвечаешь только по локальной базе знаний и не выдумываешь факты."},
                {"role": "user", "content": safe_format(KB_ANSWER_PROMPT, question=question, context=kb_context)},
            ],
        )
    except AuthenticationError:
        await safe_send(update, "OpenAI API key неверный. Обнови OPENAI_API_KEY в .env и перезапусти бота.")
        return
    except Exception:
        logger.exception("KB answer generation failed")
        await safe_send(update, "Не получилось ответить по базе знаний. Попробуй позже.")
        return

    reply = clean_bot_reply(response.choices[0].message.content or "")
    if not reply:
        reply = "В локальной базе нет подтверждения по этому вопросу."

    chat_id = chat_id_of(update)
    user_id = user_id_of(update)
    try:
        db.add_bot_response(chat_id, user_id, "kbask", reply)
        memory_manager.record_v2_bot_response(chat_id=chat_id, user_id=user_id, command="kbask", response_text=reply)
    except Exception:
        logger.exception("Failed to save kbask response")

    await safe_send(update, reply, max_len=3500)


async def remember(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    if not is_admin(update):
        await safe_send(update, admin_denied_message())
        return

    chat_id = chat_id_of(update)
    user_id = user_id_of(update)
    text = update.message.text or ""
    memory_text = text.partition(" ")[2].strip()

    if not memory_text:
        await safe_send(update, "Напиши так: /remember важный мем конфы")
        return

    if len(memory_text) > 500:
        await safe_send(update, "Слишком длинная память. Держи её короткой.")
        return

    v1_saved = False
    try:
        db.add_manual_memory(chat_id, user_id, memory_text)
        v1_saved = True
    except Exception:
        logger.exception("Failed to save manual memory")
        if not memory_manager.v2_full_transition and memory_manager.v1_memory_fallback_enabled:
            await safe_send(update, "Не получилось сохранить память. Попробуй позже.")
            return

    v2_saved = False
    try:
        memory_manager.record_v2_manual_memory(
            chat_id=chat_id,
            author_user_id=user_id,
            username=username_of(update),
            display_name=user_display_name(update),
            text=memory_text,
        )
        v2_saved = True
    except Exception:
        v2_saved = False
        logger.exception("Failed to save manual memory to v2 storage")
        if memory_manager.v2_full_transition or not memory_manager.v1_memory_fallback_enabled:
            await safe_send(update, "Не получилось сохранить v2-память. Попробуй позже.")
            return

    if v1_saved and v2_saved:
        record_audit(chat_id, user_id, "remember", details={"text": memory_text})
        await safe_send(update, "Запомнил.")
    elif v2_saved:
        record_audit(chat_id, user_id, "remember_v2_only", details={"text": memory_text})
        await safe_send(update, "Запомнил в v2. Старый SQLite fallback недоступен.")
    else:
        record_audit(chat_id, user_id, "remember_v1_only", details={"text": memory_text})
        await safe_send(update, "Запомнил в старой памяти. v2 live log временно недоступен.")


async def resolve_target_chat_id(update: Update) -> int:
    """If triggered in private chat, bridge to primary group where memory lives without moving data."""
    chat = update.effective_chat
    if not chat:
        raise RuntimeError("No chat")
    chat_id = int(chat.id)
    if chat.type == ChatType.PRIVATE:
        primary_id = await asyncio.to_thread(db.get_primary_group_chat_id)
        if primary_id and primary_id < 0:
            return primary_id
    return chat_id


def build_draft_profile_text(chat_id: int, user_id: int) -> str | None:
    messages = db.recent_user_messages(chat_id, user_id, 8)
    if not messages:
        return None

    display_name = messages[-1]["display_name"] or str(user_id)
    username = messages[-1]["username"] or ""
    header = f"Черновое досье: {display_name}"
    if username:
        header += f" (@{username})"

    lines = [
        header,
        f"Сообщений в памяти: минимум {len(messages)}",
        "LLM-профиль ещё не собран, но сырые сообщения уже сохраняются.",
        "Последние реплики:",
    ]
    for row in messages[-5:]:
        lines.append(f"- {safe_short(row['text'], 180)}")
    return "\n".join(lines)


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    chat_id = await resolve_target_chat_id(update)
    target_user_id = user_id_of(update)

    if context.args:
        raw = context.args[0].strip()
        if raw.startswith("@"):
            row = db.get_user_by_username(chat_id, raw)
            if not row:
                await safe_send(update, "Я пока не знаю такого персонажа. Пусть напишет что-нибудь в чат.")
                return
            target_user_id = int(row["user_id"])

    v2_profile = memory_manager.build_v2_profile_text(chat_id, target_user_id)
    if v2_profile:
        await safe_send(update, v2_profile)
        return
    if memory_manager.v2_full_transition:
        await safe_send(update, "V2-досье по этому участнику пока пустое.")
        return

    row = db.get_user_profile(chat_id, target_user_id)
    if not row:
        draft_profile = build_draft_profile_text(chat_id, target_user_id)
        if draft_profile:
            await safe_send(update, draft_profile)
            return
        await safe_send(
            update,
            "Досье пока пустое. Напиши несколько обычных сообщений в чат — команды не считаются.",
        )
        return

    text = (
        f"Досье: {row['display_name']} (@{row['username']})\n"
        f"Стиль: {row['style_summary']}\n"
        f"Темы: {row['frequent_topics']}\n"
        f"Мемы: {row['personal_memes']}\n"
        f"Ярлыки: {row['soft_labels']}\n"
        f"Активность: {row['energy_level']}/5\n"
        f"Токсичный стиль: {row['toxicity_style']}\n"
        f"Мемность: {row['meme_score']}/5\n"
        f"Ночной режим: {row['night_mode_behavior']}\n"
        f"Уверенность: {row['confidence_score']}"
    )
    await safe_send(update, text)


async def lore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    chat_id = await resolve_target_chat_id(update)
    v2_lore = memory_manager.build_v2_lore_text(chat_id)
    if v2_lore:
        await safe_send(update, v2_lore)
        return
    if memory_manager.v2_full_transition:
        await safe_send(update, "V2-лор для этого чата пока не сформировался.")
        return

    row = db.get_chat_memory(chat_id)
    if not row:
        await safe_send(update, "Лор пока не сформировался. Конфе нужно совершить пару исторических ошибок.")
        return

    text = (
        "Лор конфы:\n"
        f"Настроение: {row['mood_today']}\n"
        f"Хаос: {row['chaos_level']}/5\n"
        f"Тема дня: {row['main_topic_today']}\n"
        f"Главный клоун дня: {row['main_clown_today']}\n"
        f"Мем дня: {row['meme_of_the_day']}\n"
        f"Мемы недели: {row['weekly_memes']}\n"
        f"Драма: {row['recent_drama']}\n"
        f"Фразы: {row['local_phrases']}\n"
        f"Артефакты: {row['sacred_artifacts']}\n"
        f"Мифология: {row['chat_mythology']}"
    )
    await safe_send(update, text)


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    chat_id = await resolve_target_chat_id(update)
    wait_seconds = check_chat_cooldown(summary_rate_limit, chat_id, SUMMARY_COOLDOWN_SECONDS)
    if wait_seconds > 0:
        await safe_send(update, f"Летописец отдыхает. Повтори через {wait_seconds} сек.")
        return

    try:
        await memory_manager.maybe_update_memory(chat_id)
        context_text = memory_manager.build_chat_context(chat_id, 100)
        kb_context = ""
        if knowledge_base.exists():
            recent = db.recent_messages(chat_id, 20)
            query = build_kb_query(" ".join(row["text"] for row in recent[-10:]), context_text)
            kb_context = knowledge_base.build_context(query, limit=3, max_chars=2500)
        if kb_context:
            context_text = safe_short(context_text + "\n\nKNOWLEDGE BASE:\n" + kb_context, 14000)

        response = await openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.5,
            max_tokens=600,
            **_llm_kwargs(),
                messages=[
                {"role": "system", "content": CORE_STYLE_SYSTEM},
                {"role": "user", "content": safe_format(SUMMARY_PROMPT, context=context_text)},
            ],
        )
    except AuthenticationError:
        summary_rate_limit[chat_id] = 0
        logger.error("Summary generation failed: invalid OPENAI_API_KEY")
        await safe_send(update, "OpenAI API key неверный. Обнови OPENAI_API_KEY в .env и перезапусти бота.")
        return
    except Exception:
        summary_rate_limit[chat_id] = 0
        logger.exception("Summary generation failed")
        await safe_send(update, "Летопись не сложилась. Попробуй позже.")
        return

    reply = clean_bot_reply(response.choices[0].message.content or "")
    if not reply:
        reply = "Летопись не сложилась. Видимо, конфа сегодня превзошла письменность."

    try:
        db.add_bot_response(chat_id, None, "summary", reply)
        memory_manager.record_v2_bot_response(chat_id=chat_id, user_id=None, command="summary", response_text=reply)
    except Exception:
        logger.exception("Failed to save summary response")

    await safe_send(update, reply)


async def future(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    chat_id = await resolve_target_chat_id(update)
    user_id = user_id_of(update)

    wait_seconds = check_user_cooldown(
        future_rate_limit,
        chat_id,
        user_id,
        FUTURE_COOLDOWN_SECONDS,
    )
    if wait_seconds > 0:
        await safe_send(update, f"Оракул устал именно от тебя. Повтори через {wait_seconds} сек.", max_len=1000)
        return

    try:
        await memory_manager.maybe_update_memory(chat_id)

        context_text = memory_manager.build_context_for_user(chat_id, user_id, MAX_RECENT_MESSAGES)
        if knowledge_base.exists():
            recent_user = db.recent_user_messages(chat_id, user_id, 10)
            query_seed = " ".join(row["text"] for row in recent_user[-5:]) or context_text
            kb_context = knowledge_base.build_context(build_kb_query(query_seed), limit=3, max_chars=2200)
            if kb_context:
                context_text = safe_short(context_text + "\n\nKNOWLEDGE BASE:\n" + kb_context, 14000)
        previous = db.recent_bot_responses(chat_id, MAX_RECENT_BOT_RESPONSES)

        last_reason = ""
        chosen = ""

        for _attempt in range(REGENERATION_ATTEMPTS):
            extra = ""
            if last_reason:
                extra = (
                    "\n\nПредыдущая попытка была отклонена: "
                    f"{last_reason}. Напиши иначе, с другим началом, другим ритмом и другой шуткой."
                )

            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.75,
                max_tokens=600,
                **_llm_kwargs(),
                messages=[
                    {"role": "system", "content": CORE_STYLE_SYSTEM},
                    {"role": "user", "content": safe_format(FUTURE_PROMPT, context=context_text + extra)},
                ],
            )

            candidate = clean_bot_reply(response.choices[0].message.content or "")
            if not candidate:
                continue

            too_similar, reason = is_too_similar(candidate, previous)
            if not too_similar:
                chosen = candidate
                break

            last_reason = reason
            chosen = candidate

    except AuthenticationError:
        future_rate_limit[(chat_id, user_id)] = 0
        logger.error("Future generation failed: invalid OPENAI_API_KEY")
        await safe_send(update, "OpenAI API key неверный. Обнови OPENAI_API_KEY в .env и перезапусти бота.", max_len=1000)
        return
    except Exception:
        future_rate_limit[(chat_id, user_id)] = 0
        logger.exception("Future generation failed")
        await safe_send(update, "Оракул завис. Попробуй позже.", max_len=1000)
        return

    if not chosen:
        if db.count_messages(chat_id) < 5:
            chosen = f"🔮 {user_display_name(update)}, база только что обновилась и твоё досье пока чистое! Напиши в чате пару реплик или задай вопрос, чтобы Оракул настроил свои радары."
        else:
            chosen = "Оракул завис. Видимо, будущее посмотрело на конфу и решило не загружаться."

    try:
        db.add_bot_response(chat_id, user_id, "future", chosen)
        memory_manager.record_v2_bot_response(chat_id=chat_id, user_id=user_id, command="future", response_text=chosen)
    except Exception:
        logger.exception("Failed to save future response")

    await safe_send(update, chosen, max_len=1000)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return

    chat_id = await resolve_target_chat_id(update)
    user_id = user_id_of(update)
    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await safe_send(update, "Напиши так: /ask твой вопрос")
        return

    wait_seconds = check_user_cooldown(ask_rate_limit, chat_id, user_id, ASK_COOLDOWN_SECONDS)
    if wait_seconds > 0:
        await safe_send(update, f"Слишком быстро. Повтори через {wait_seconds} сек.", max_len=1000)
        return

    try:
        await memory_manager.maybe_update_memory(chat_id)
        chat_context = memory_manager.build_chat_context(chat_id, 80)
        kb_context = ""
        if knowledge_base.exists():
            kb_context = knowledge_base.build_context(build_kb_query(question, chat_context), limit=KB_SEARCH_LIMIT, max_chars=5000)

        from web_search import live_web_context
        live_web = await live_web_context(question, force=True)
        if live_web:
            chat_context = safe_short(
                chat_context + "\n\nАКТУАЛЬНЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА (только что получено):\n" + live_web,
                14000,
            )
    except Exception:
        logger.exception("Failed to build /ask context")
        await safe_send(update, "Не получилось собрать контекст. Попробуй позже.")
        return

    msg_list = [
        {"role": "system", "content": "Ты полезный Telegram-бот. Отвечай точно, кратко и без выдумки."},
        {"role": "user", "content": safe_format(ASK_PROMPT, question=question, chat_context=chat_context, kb_context=kb_context or "No KB matches found.")},
    ]
    try:
        try:
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.35,
                **({"tools": AGENT_TOOLS, "tool_choice": "auto"} if _supports_native_tools() else {}),
                **_llm_kwargs(),
                messages=msg_list,
            )
        except (BadRequest, TypeError):
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.35,
                **_llm_kwargs(),
                messages=msg_list,
            )

        if response.choices and response.choices[0].message.tool_calls:
            t_msg = response.choices[0].message
            msg_list.append(t_msg)
            for tc in t_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    fn_args = {}
                t_res = await execute_agent_tool(chat_id, user_id, fn_name, fn_args)
                msg_list.append({"role": "tool", "tool_call_id": tc.id, "content": str(t_res)})

            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.35,
                **_llm_kwargs(),
                messages=msg_list,
            )
    except AuthenticationError:
        ask_rate_limit[(chat_id, user_id)] = 0
        await safe_send(update, "OpenAI API key неверный. Обнови OPENAI_API_KEY в .env и перезапусти бота.")
        return
    except Exception:
        ask_rate_limit[(chat_id, user_id)] = 0
        logger.exception("Ask generation failed")
        await safe_send(update, "Не получилось ответить. Попробуй позже.")
        return

    reply = clean_bot_reply(response.choices[0].message.content or "")
    if not reply:
        reply = "Не получилось собрать внятный ответ."

    try:
        db.add_bot_response(chat_id, user_id, "ask", reply)
        memory_manager.record_v2_bot_response(chat_id=chat_id, user_id=user_id, command="ask", response_text=reply)
    except Exception:
        logger.exception("Failed to save ask response")

    await safe_send(update, reply, max_len=3500)


async def kbimport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    if not is_admin(update):
        await safe_send(update, admin_denied_message())
        return
    if not knowledge_base.exists():
        await safe_send(update, f"База знаний не найдена по пути: {knowledge_base.base_dir}")
        return

    reply = update.message.reply_to_message
    arg_text = " ".join(context.args).strip() if context.args else ""
    target_desc = ""

    try:
        knowledge_base.ensure_inbox()
        if arg_text:
            target_desc = arg_text
            result = await asyncio.to_thread(knowledge_base.add, arg_text)
        elif reply and reply.text:
            url = extract_first_url(reply.text)
            if url:
                target_desc = url
                result = await asyncio.to_thread(knowledge_base.add, url)
            else:
                file_name = f"telegram-note-{chat_id_of(update)}-{reply.message_id}.txt"
                note_path = knowledge_base.inbox_dir / file_name
                note_path.write_text(reply.text, encoding="utf-8")
                target_desc = note_path.name
                result = await asyncio.to_thread(knowledge_base.add, str(note_path))
        elif reply and reply.document:
            original_name = reply.document.file_name or f"document-{reply.message_id}.bin"
            target_path = knowledge_base.inbox_dir / original_name
            telegram_file = await context.bot.get_file(reply.document.file_id)
            await telegram_file.download_to_drive(custom_path=str(target_path))
            target_desc = target_path.name
            result = await asyncio.to_thread(knowledge_base.add, str(target_path))
        elif reply and reply.audio:
            ext = Path(reply.audio.file_name or "audio.mp3").suffix or ".mp3"
            target_path = knowledge_base.inbox_dir / f"telegram-audio-{chat_id_of(update)}-{reply.message_id}{ext}"
            telegram_file = await context.bot.get_file(reply.audio.file_id)
            await telegram_file.download_to_drive(custom_path=str(target_path))
            target_desc = target_path.name
            result = await asyncio.to_thread(knowledge_base.add, str(target_path))
        elif reply and reply.voice:
            target_path = knowledge_base.inbox_dir / f"telegram-voice-{chat_id_of(update)}-{reply.message_id}.ogg"
            telegram_file = await context.bot.get_file(reply.voice.file_id)
            await telegram_file.download_to_drive(custom_path=str(target_path))
            target_desc = target_path.name
            result = await asyncio.to_thread(knowledge_base.add, str(target_path))
        elif reply and reply.video:
            ext = Path(reply.video.file_name or "video.mp4").suffix or ".mp4"
            target_path = knowledge_base.inbox_dir / f"telegram-video-{chat_id_of(update)}-{reply.message_id}{ext}"
            telegram_file = await context.bot.get_file(reply.video.file_id)
            await telegram_file.download_to_drive(custom_path=str(target_path))
            target_desc = target_path.name
            result = await asyncio.to_thread(knowledge_base.add, str(target_path))
        else:
            await safe_send(update, "Сделай reply на текст/документ/аудио/видео или передай URL: /kbimport <url>")
            return
    except Exception:
        logger.exception("KB import failed")
        await safe_send(update, "Импорт в базу знаний не удался.")
        return

    if result.get("code") != 0:
        await safe_send(update, f"Импорт не удался: {safe_short(result.get('output', ''), 1200)}")
        return

    try:
        await asyncio.to_thread(knowledge_base.rebuild_index)
    except Exception:
        logger.exception("Failed to rebuild KB index after import")

    output = result.get("output", "").strip()
    try:
        parsed = json.loads(output) if output.startswith("{") else None
    except json.JSONDecodeError:
        parsed = None
    status_text = parsed.get("status") if isinstance(parsed, dict) else "imported"
    record_audit(
        chat_id_of(update),
        user_id_of(update),
        "kbimport",
        details={"target": target_desc, "status": status_text},
    )
    await safe_send(update, f"Импортировано в KB: {target_desc}\nstatus: {status_text}")


async def import_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to import Telegram result.json history by replying to a document."""
    if not update.message:
        return
    if not await ensure_allowed_chat(update):
        return
    if not is_admin(update):
        await safe_send(update, admin_denied_message())
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await safe_send(update, "Сделай Reply (ответить) на файл result.json и напиши: /import_history [дней, например 365]")
        return

    days = int(context.args[0]) if context.args and context.args[0].isdigit() else 365
    chat_id = await resolve_target_chat_id(update)
    if chat_id > 0:
        target_id_str = os.getenv("PRIMARY_GROUP_CHAT_ID", "").strip()
        chat_id = int(target_id_str) if (target_id_str and target_id_str.lstrip("-").isdigit()) else (list(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else -1000)

    await safe_send(update, f"⏳ Загружаю файл {reply.document.file_name} и импортирую последние {days} дней в чат {chat_id}...")
    try:
        temp_dir = Path("/data/tmp") if Path("/data").exists() else Path("exports/tmp")
        temp_dir.mkdir(parents=True, exist_ok=True)
        target_path = temp_dir / (reply.document.file_name or "result.json")
        telegram_file = await context.bot.get_file(reply.document.file_id)
        await telegram_file.download_to_drive(custom_path=str(target_path))

        import subprocess
        import sys
        cmd = [
            sys.executable,
            "scripts/import_telegram_json.py",
            "--json",
            str(target_path),
            "--db",
            DATABASE_PATH,
            "--chat-id",
            str(chat_id),
            "--days",
            str(days),
        ]
        proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            await safe_send(update, f"✅ История успешно импортирована!\n{safe_short(proc.stdout, 1500)}")
        else:
            await safe_send(update, f"❌ Ошибка импорта:\n{safe_short(proc.stderr or proc.stdout, 1500)}")
        try:
            target_path.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("import_history failed")
        await safe_send(update, f"❌ Ошибка при обработке файла: {exc}")


async def fix_group_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to move all imported history from private chat ID to the group chat ID."""
    if not update.message or not await ensure_allowed_chat(update) or not is_admin(update):
        return
    target_id_str = os.getenv("PRIMARY_GROUP_CHAT_ID", "").strip()
    default_group_id = int(target_id_str) if (target_id_str and target_id_str.lstrip("-").isdigit()) else (list(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else -1000)
    group_id = int(context.args[0]) if context.args and context.args[0].lstrip("-").isdigit() else default_group_id
    try:
        with db.connect() as conn:
            conn.execute("""
                INSERT INTO users (chat_id, user_id, username, display_name, first_seen, last_seen)
                SELECT ?, user_id, username, display_name, first_seen, last_seen
                FROM users WHERE chat_id > 0
                ON CONFLICT(chat_id, user_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    last_seen=excluded.last_seen
            """, (group_id,))
            conn.execute("DELETE FROM users WHERE chat_id > 0")
            c_msg = conn.execute("UPDATE raw_messages SET chat_id = ? WHERE chat_id > 0", (group_id,)).rowcount
            conn.commit()
        await safe_send(update, f"✅ Успешно перенесено {c_msg} сообщений в групповой чат {group_id}!")
    except Exception as exc:
        await safe_send(update, f"❌ Ошибка переноса: {exc}")


async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to switch OPENAI_MODEL on the fly without restarting container."""
    global OPENAI_MODEL
    if not update.message or not await ensure_allowed_chat(update) or not is_admin(update):
        return
    if not context.args:
        await safe_send(update, f"Текущая модель: {OPENAI_MODEL}\nИспользование: /set_model [glm-5.1 / llama70b / qwen3_235b]")
        return
    new_m = context.args[0].strip()
    OPENAI_MODEL = new_m
    memory_manager.model = new_m
    try:
        db.set_meta(await resolve_target_chat_id(update), "active_model", new_m)
    except Exception:
        pass
    await safe_send(update, f"✅ Модель успешно переключена на: {OPENAI_MODEL}")


async def set_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to switch prompt presets on the fly and hot-reload in memory.

    Never copies over prompts.py (Amvera wipes non-/data files on rebuild).
    Persists choice under /data/style_choice.txt.
    """
    if not update.message or not await ensure_allowed_chat(update) or not is_admin(update):
        return
    if not context.args:
        await safe_send(update, "Использование: /set_style [unrestricted / smart / toxic / clean]")
        return
    style = context.args[0].lower().strip()
    module_name = _STYLE_MODULES.get(style)
    if not module_name:
        await safe_send(update, "❌ Неизвестный стиль. Выбери из: unrestricted / smart / toxic / clean")
        return

    try:
        _apply_style_module(module_name)
        try:
            _STYLE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STYLE_STATE_PATH.write_text(module_name, encoding="utf-8")
        except Exception:
            logger.exception("Failed to persist style choice to %s", _STYLE_STATE_PATH)
        await safe_send(
            update,
            f"✅ Стиль успешно переключён на {style} ({module_name}) и горячо перезагружен в памяти!",
        )
    except Exception as exc:
        await safe_send(update, f"❌ Ошибка переключения стиля: {exc}")


async def clean_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to clean up junk/duplicate files from root workspace without needing Online IDE."""
    if not update.message or not await ensure_allowed_chat(update) or not is_admin(update):
        return

    await safe_send(update, "⏳ Начинаю генеральную уборку мусора в репозитории...")
    try:
        import shutil
        removed_count = 0
        removed_names = []
        keep_files = {
            "bot.py", "prompts.py", "memory.py", "database.py", "utils.py", "config.py",
            "amvera.yml", "amvera.yaml", "requirements.txt", "runtime.txt", "main.py",
            "prompts_smart.py", "prompts_v2_toxic.py", "prompts_v3_unrestricted.py",
            "prompts_v1_backup.py", "prompts_v2_2.py", "README.md", "README_FINAL.md",
            "README_TOXIC_v23.md", "AMVERA_DEPLOY.md", "SOULFUL_ENGINEER_PROMPT.md"
        }
        keep_dirs = {"scripts", "AI_Knowledge_Base", "deploy", "docs", "src", "tests", "exports", "attached_assets"}

        for item in Path(".").iterdir():
            if item.name == "." or item.name == ".." or item.name == ".git" or item.name == "data":
                continue
            if item.is_file():
                if item.name not in keep_files and (
                    "download" in item.name
                    or item.name.startswith("test-")
                    or item.name.endswith(".html")
                    or item.name.endswith(".pyc")
                    or "(" in item.name
                    or item.name.startswith("predskazbot_v")
                ):
                    try:
                        item.unlink()
                        removed_count += 1
                        removed_names.append(item.name)
                    except Exception:
                        pass
            elif item.is_dir():
                if item.name not in keep_dirs and (
                    "download" in item.name
                    or item.name == "__pycache__"
                    or "(" in item.name
                    or item.name.startswith("predskazbot_v")
                ):
                    try:
                        shutil.rmtree(item)
                        removed_count += 1
                        removed_names.append(item.name + "/")
                    except Exception:
                        pass

        summary_del = ", ".join(removed_names[:15]) + ("..." if len(removed_names) > 15 else "")
        await safe_send(update, f"✅ Уборка завершена!\n🗑️ Удалено мусора: {removed_count} объектов ({summary_del})\n📂 В корне остались только чистые рабочие файлы.")
    except Exception as exc:
        logger.exception("clean_repo failed")
        await safe_send(update, f"❌ Ошибка уборки: {exc}")


AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_chat_history",
            "description": "Искать старые сообщения и споры в базе истории конфы (15 000+ сообщений за все годы). Используй при вопросах 'когда говорили', 'кто писал про X', 'найди сообщения про Y'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключевое слово или фраза для поиска в тексте сообщений (например 'крипта', 'рукоятка', 'тула').",
                    },
                    "username": {
                        "type": "string",
                        "description": "Опционально: никнейм автора сообщения без @ (например 'seksovsex').",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Сколько результатов вернуть (1 до 15). По умолчанию 8.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_stats",
            "description": "Получить точную статистику активности, количество сообщений, даты первого/последнего появления и досье участника конфы.",
            "parameters": {
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Никнейм (без @) или имя пользователя в Telegram.",
                    }
                },
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_web_data",
            "description": "Выход в интернет для получения свежей информации в реальном времени: погода, курс валют/криптовалют, новости, справка по фактам.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Запрос для поиска в интернете (например 'погода в Москве', 'курс биткоина к доллару', 'что такое квантовый компьютер').",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_sacred_lore",
            "description": "Сохранить важный факт, локальный мем или цитату в священную ручную память конфы (будет учитываться во всех будущих прогнозах и летописях).",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_text": {
                        "type": "string",
                        "description": "Текст факта или мема для сохранения (до 300 символов).",
                    }
                },
                "required": ["memory_text"],
            },
        },
    },
]


async def execute_agent_tool(chat_id: int, user_id: int, tool_name: str, arguments: dict) -> str:
    """Execute tool requested by LLM inside agentic loop."""
    try:
        if tool_name == "search_chat_history":
            query = str(arguments.get("query", "")).strip()
            uname = arguments.get("username")
            if not query or len(query) < 2:
                return "Ошибка: поисковый запрос должен содержать минимум 2 символа."
            lmt = min(max(int(arguments.get("limit", 8)), 1), 15)
            rows = await asyncio.to_thread(db.search_history_messages, chat_id, query, uname, lmt)
            if not rows:
                return f"По запросу '{query}' в архиве сообщений не найдено."
            res = [f"[{r.get('created_at','')[:10]}] {r.get('display_name')}: {r.get('text')}" for r in rows]
            return "Найдено в архиве конфы:\n" + "\n".join(res)

        elif tool_name == "get_user_stats":
            uname = str(arguments.get("username", "")).strip()
            if not uname:
                return "Ошибка: не указан никнейм."
            stats = await asyncio.to_thread(db.get_user_full_statistics, chat_id, uname)
            if not stats:
                return f"Участник '{uname}' в базе сообщений не найден."
            u_id = stats.get("user_id")
            prof = await asyncio.to_thread(db.get_user_profile, chat_id, u_id) if u_id else None
            out = f"Статистика по @{stats.get('username')} ({stats.get('display_name')}):\nСообщений в базе: {stats.get('total_messages')}\nВпервые замечен: {stats.get('first_seen','')[:10]}\nПоследняя активность: {stats.get('last_seen','')[:10]}"
            if prof:
                out += f"\nСтиль: {prof.get('style_summary')}\nТемы: {prof.get('frequent_topics')}\nМемы: {prof.get('personal_memes')}\nУверенность досье: {prof.get('confidence_score')}"
            return out

        elif tool_name == "fetch_web_data":
            query = str(arguments.get("query", "")).strip()
            if not query:
                return "Ошибка: пустой запрос."
            from web_search import live_web_context
            res = await live_web_context(query, force=True)
            return res or f"По запросу '{query}' в интернете свежих данных не получено."

        elif tool_name == "add_sacred_lore":
            mem_text = str(arguments.get("memory_text", "")).strip()
            if not mem_text:
                return "Ошибка: пустой текст памяти."
            await asyncio.to_thread(db.add_manual_memory, chat_id, user_id, mem_text[:300])
            return f"✅ Факт успешно сохранён в священную ручную память: '{mem_text[:300]}'"

        else:
            return f"Неизвестный инструмент: {tool_name}"
    except Exception as exc:
        logger.exception("Agent tool execution failed: %s", tool_name)
        return f"Ошибка при выполнении инструмента {tool_name}: {exc}"


def _supports_native_tools() -> bool:
    """GLM/LLaMA often emit tool-call syntax as plain text instead of
    structured tool_calls. Only trust tools= for model families known to
    return proper structured tool_calls."""
    model = OPENAI_MODEL.lower()
    return any(model.startswith(p) for p in ("gpt-", "o1-", "o3-", "openai/"))


def _message_addresses_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True if the user is talking to the bot (mention / name / slang)."""
    msg = update.message
    if not msg or not msg.text:
        return False

    bot = context.bot
    bot_username = (bot.username or "").lower()
    bot_id = bot.id

    for ent in msg.entities or []:
        et = getattr(ent, "type", None)
        et_val = getattr(et, "value", et)
        if et_val == "mention":
            mention = msg.text[ent.offset : ent.offset + ent.length].lower()
            if bot_username and mention == f"@{bot_username}":
                return True
        elif et_val == "text_mention":
            if ent.user and ent.user.id == bot_id:
                return True

    text_lower = msg.text.lower().replace("ё", "е")
    # Slang / brand / diminutives used in the confa
    trigger_words = [
        "хуебот",
        "хуеботик",
        "еблобот",
        "еблоботик",
        "еблбот",
        "hyebot",
        "hye bot",
        "hye-bot",
        "ботик",
        "ботяра",
        "ботя",
        "сексялка",
        "бафик",
        "предсказалка",
        "предсказбот",
        "predskazbot",
        "оракул",
        "seksyalka",
        "seksyalka_predskazalka_bot",
    ]
    if bot_username:
        trigger_words.append(f"@{bot_username}")
        trigger_words.append(bot_username)

    if any(w in text_lower for w in trigger_words):
        return True

    # whole-word "бот" / "bot" (not "работа", "ботан", "робот")
    if re.search(r"(^|[\s,.:;!?«\"'(])бот([\s,.:;!?»\"')]|$)", text_lower):
        return True
    if re.search(r"(^|[\s,.:;!?\"'(])bot([\s,.:;!?\"')]|$)", text_lower):
        return True

    # any token that *is* a bot-call: *бот / *bot (еблобот, хуебот, mybot…)
    # but reject long normal words containing бот mid-stem via length/suffix check
    for tok in re.findall(r"[a-zа-я0-9_@]+", text_lower):
        if tok in {"работа", "работать", "работаю", "ботаник", "ботан", "робот", "роботы"}:
            continue
        if tok.endswith("бот") or tok.endswith("bot") or tok.endswith("ботик"):
            if 3 <= len(tok) <= 24:
                return True

    return False


async def chat_participant_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = await resolve_target_chat_id(update)
    user_id = user_id_of(update)

    participant_cd = int(os.getenv("PARTICIPANT_COOLDOWN_SECONDS", "3"))
    if check_user_cooldown(participant_rate_limit, chat_id, user_id, participant_cd) > 0:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        # Hot path: never block on memory curator (extra LLM call).
        ctx_msgs = int(os.getenv("PARTICIPANT_CONTEXT_MESSAGES", "15"))
        chat_context = memory_manager.build_chat_context(chat_id, ctx_msgs)
        user_profile = memory_manager.build_context_for_user(chat_id, user_id, 5)

        # Live web: on by intent (weather/курс/факт), or forced via env.
        # LIVE_WEB_ON_PARTICIPANT=0 disables; =1 always tries; default=auto.
        _lw_mode = os.getenv("LIVE_WEB_ON_PARTICIPANT", "auto").strip().lower()
        _want_web = False
        if _lw_mode in ("1", "true", "yes", "on"):
            _want_web = True
        elif _lw_mode in ("0", "false", "no", "off"):
            _want_web = False
        else:
            try:
                from web_search import needs_live_web
                _want_web = needs_live_web(update.message.text)
            except Exception:
                _want_web = False
        if _want_web:
            from web_search import live_web_context
            live_web = await live_web_context(update.message.text, force=False)
            if live_web:
                chat_context = safe_short(
                    chat_context + "\n\nАКТУАЛЬНЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА (только что получено):\n" + live_web,
                    14000,
                )

        max_out = int(os.getenv("PARTICIPANT_MAX_TOKENS", "280"))
        use_tools = _supports_native_tools()
        msg_list = [
            {"role": "system", "content": CORE_STYLE_SYSTEM},
            {
                "role": "user",
                "content": safe_format(
                    PARTICIPANT_PROMPT,
                    user_name=user_display_name(update),
                    message_text=update.message.text,
                    chat_context=chat_context,
                    user_profile=user_profile,
                ),
            },
        ]
        try:
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.8,
                max_tokens=max_out,
                **({"tools": AGENT_TOOLS, "tool_choice": "auto"} if use_tools else {}),
                **_llm_kwargs(),
                messages=msg_list,
            )
        except (BadRequest, TypeError):
            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.8,
                max_tokens=max_out,
                **_llm_kwargs(),
                messages=msg_list,
            )

        if use_tools and response.choices and response.choices[0].message.tool_calls:
            t_msg = response.choices[0].message
            msg_list.append(t_msg)
            for tc in t_msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    fn_args = {}
                t_res = await execute_agent_tool(chat_id, user_id, fn_name, fn_args)
                msg_list.append({"role": "tool", "tool_call_id": tc.id, "content": str(t_res)})

            response = await openai_client.chat.completions.create(
                model=OPENAI_MODEL,
                temperature=0.8,
                max_tokens=max_out,
                **_llm_kwargs(),
                messages=msg_list,
            )
    except AuthenticationError:
        logger.error("Participant reply failed: invalid OPENAI_API_KEY")
        return
    except Exception:
        logger.exception("Participant reply failed")
        return

    reply = clean_bot_reply(response.choices[0].message.content or "")
    if not reply:
        return

    try:
        await asyncio.to_thread(db.add_bot_response, chat_id, user_id, "participant", reply)
        memory_manager.record_v2_bot_response(chat_id=chat_id, user_id=user_id, command="participant", response_text=reply)
    except Exception:
        logger.exception("Failed to save participant response")

    try:
        await update.message.reply_text(safe_short(reply, 3500))
    except Exception:
        await safe_send(update, reply, max_len=3500)


async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if update.effective_user and update.effective_user.is_bot:
        return

    chat = update.effective_chat
    if not chat:
        return

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.PRIVATE}:
        return

    text = update.message.text.strip()[:2000]
    if not text:
        return

    try:
        chat_id = chat_id_of(update)
        user_id = user_id_of(update)
    except RuntimeError:
        logger.warning("Skipped update without chat or user")
        return
    if not is_allowed_chat_id(chat_id):
        logger.warning("Skipped message from non-allowlisted chat_id=%s", chat_id)
        return

    display_name = user_display_name(update)
    username = username_of(update)
    mentions = extract_mentions(text)
    reply_to_message_id = update.message.reply_to_message.message_id if update.message.reply_to_message else None

    try:
        v1_saved = False
        await asyncio.to_thread(db.add_message, chat_id, user_id, username, display_name, text, mentions)
        v1_saved = True
    except Exception:
        v1_saved = False
        logger.exception("Failed to save incoming message to v1 SQLite")
        if not memory_manager.v2_full_transition and memory_manager.v1_memory_fallback_enabled:
            return

    try:
        v2_saved = True
        memory_manager.record_v2_message(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            display_name=display_name,
            text=text,
            mentions=mentions,
            telegram_message_id=update.message.message_id,
            telegram_thread_id=getattr(update.message, "message_thread_id", None),
            reply_to_message_id=reply_to_message_id,
            chat_title=chat.title or "",
            chat_type=str(chat.type),
        )
    except Exception:
        v2_saved = False
        logger.exception("Failed to save incoming message to v2 live event log")
        if memory_manager.v2_full_transition or not memory_manager.v1_memory_fallback_enabled:
            return

    if not v1_saved and not v2_saved:
        logger.warning("Message was not saved in either v1 or v2 storage")

    should_reply = False
    is_reply_to_bot = bool(
        update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    )
    has_trigger = _message_addresses_bot(update, context)

    if chat.type == ChatType.PRIVATE:
        should_reply = True
    elif is_reply_to_bot or has_trigger:
        should_reply = True
    else:
        spontaneous_chance = float(os.getenv("SPONTANEOUS_REPLY_CHANCE", "0.05"))
        if (
            len(text) > 15
            and spontaneous_chance > 0
            and check_chat_cooldown(spontaneous_rate_limit, chat_id, 120) == 0
        ):
            import random

            if random.random() < spontaneous_chance:
                should_reply = True
            else:
                spontaneous_rate_limit.pop(chat_id, None)

    if should_reply:
        # Answer first — never wait on memory curator for a live reply.
        await chat_participant_reply(update, context)
    elif v1_saved:
        try:
            await memory_manager.maybe_update_memory(chat_id)
        except Exception:
            logger.exception("Memory update failed")


def ensure_event_loop() -> None:
    """Create a main-thread asyncio event loop when Python does not provide one.

    Python 3.14 no longer guarantees that asyncio.get_event_loop() returns a
    default loop. python-telegram-bot still expects one during run_polling(), so
    Windows/local runs need an explicit loop before Application starts polling.
    """
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def validate_env() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY / VENICE_API_KEY / MOONSHOT_API_KEY / OPENROUTER_API_KEY")
    if missing:
        # try to give friendly hint
        hint = ""
        try:
            import config as _cfg
            hint = f"\nDetected provider config: {_cfg.provider_info()}"
        except Exception:
            pass
        raise RuntimeError(
            "Missing env variables: "
            + ", ".join(missing)
            + ". Create .env from .env.example and fill the tokens."
            + hint
            + "\nRun: python scripts/setup_env.py  or  python scripts/check_provider.py"
        )


def auto_import_json_on_startup() -> None:
    """Automatically import result.json on startup if database is empty."""
    try:
        target_id_str = os.getenv("PRIMARY_GROUP_CHAT_ID", "").strip()
        chat_id = int(target_id_str) if (target_id_str and target_id_str.lstrip("-").isdigit()) else (list(ALLOWED_CHAT_IDS)[0] if ALLOWED_CHAT_IDS else -1000)
        count = db.count_messages(chat_id)
        if count > 20:
            return

        candidate_paths = [
            Path("/data/result.json"),
            Path("result.json"),
            Path("exports/result.json"),
            Path("attached_assets/result.json"),
        ]
        found_json = None
        for p in candidate_paths:
            if p.exists() and p.stat().st_size > 100:
                found_json = p
                break

        if not found_json:
            return

        logger.info("Found %s and database count is %d -> running auto-import...", found_json, count)
        import subprocess
        import sys
        days_to_import = int(os.getenv("IMPORT_HISTORY_DAYS", "365"))
        cmd = [
            sys.executable,
            "scripts/import_telegram_json.py",
            "--json",
            str(found_json),
            "--db",
            DATABASE_PATH,
            "--chat-id",
            str(chat_id),
            "--days",
            str(days_to_import),
        ]
        subprocess.run(cmd, check=True)
        logger.info("Auto-import completed successfully!")
    except Exception:
        logger.exception("Auto-import of result.json failed")


async def periodic_rate_limit_cleanup() -> None:
    """Background task to sweep expired rate limit entries every 6 hours."""
    while True:
        try:
            await asyncio.sleep(21600)
            now = time.time()
            for bucket in (future_rate_limit, ask_rate_limit, participant_rate_limit):
                expired = [k for k, v in bucket.items() if v < now]
                for k in expired:
                    bucket.pop(k, None)
            expired_spont = [k for k, v in spontaneous_rate_limit.items() if v < now - 3600]
            for k in expired_spont:
                spontaneous_rate_limit.pop(k, None)
            logger.info("Periodic rate limit sweep completed.")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in periodic_rate_limit_cleanup")


async def post_init(application: Application) -> None:
    asyncio.create_task(periodic_rate_limit_cleanup())


def main() -> None:
    validate_env()
    ensure_event_loop()
    db.init()
    auto_import_json_on_startup()

    # Amvera Moscow often has flaky paths to api.telegram.org — raise timeouts
    # and use a separate long-poll client so getUpdates doesn't die on 5s default.
    connect_t = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
    read_t = float(os.getenv("TELEGRAM_READ_TIMEOUT", "30"))
    write_t = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "30"))
    pool_t = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))
    updates_read_t = float(os.getenv("TELEGRAM_GET_UPDATES_READ_TIMEOUT", "45"))

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=connect_t,
        read_timeout=read_t,
        write_timeout=write_t,
        pool_timeout=pool_t,
    )
    get_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=connect_t,
        read_timeout=updates_read_t,
        write_timeout=write_t,
        pool_timeout=pool_t,
    )

    # Timeouts live ONLY on HTTPXRequest instances — PTB forbids mixing
    # .request(...) with builder .connect_timeout()/.read_timeout()/etc.
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("future", future))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("lore", lore))
    app.add_handler(CommandHandler("remember", remember))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("privacy", privacy))
    app.add_handler(CommandHandler("export_me", export_me))
    app.add_handler(CommandHandler("delete_me", delete_me))
    app.add_handler(CommandHandler("forget", forget))
    app.add_handler(CommandHandler("kbstatus", kbstatus))
    app.add_handler(CommandHandler("kbsearch", kbsearch))
    app.add_handler(CommandHandler("kbask", kbask))
    app.add_handler(CommandHandler("kbimport", kbimport))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("v2status", v2status))
    app.add_handler(CommandHandler("import_history", import_history))
    app.add_handler(CommandHandler("fix_group_id", fix_group_id))
    app.add_handler(CommandHandler("set_model", set_model))
    app.add_handler(CommandHandler("set_style", set_style))
    app.add_handler(CommandHandler("clean_repo", clean_repo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))

    mode = "v2-full" if memory_manager.v2_full_transition else "bridge"
    logger.info("PredskazBot started with v2 mode=%s", mode)

    # Retry loop: Amvera↔Telegram connect timeouts must not leave the process dead.
    max_start_attempts = int(os.getenv("TELEGRAM_START_RETRIES", "0"))  # 0 = forever
    attempt = 0
    while True:
        attempt += 1
        try:
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
                close_loop=False,
            )
            break
        except (TimedOut, NetworkError) as exc:
            logger.error(
                "Telegram start/poll timed out (attempt %s): %s — retry in %ss",
                attempt,
                exc,
                min(5 * attempt, 60),
            )
            if max_start_attempts and attempt >= max_start_attempts:
                raise
            time.sleep(min(5 * attempt, 60))
        except Exception:
            logger.exception("run_polling crashed (attempt %s) — retry in 15s", attempt)
            if max_start_attempts and attempt >= max_start_attempts:
                raise
            time.sleep(15)


if __name__ == "__main__":
    main()
