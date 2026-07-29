import re
from difflib import SequenceMatcher
from typing import Iterable


_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_@#]+")


def normalize_text(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"[^a-zа-я0-9_@#\s-]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_text(text))


def jaccard_similarity(a: str, b: str) -> float:
    set_a = set(words(a))
    set_b = set(words(b))
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def sequence_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def first_words_signature(text: str, count: int = 6) -> str:
    return " ".join(words(text)[:count])


def extract_mentions(text: str) -> list[str]:
    return sorted(set(re.findall(r"@[A-Za-z0-9_]{3,32}", text)))


async def fetch_live_web_info(user_text: str) -> str:
    """Backward-compatible wrapper around web_search.live_web_context."""
    try:
        from web_search import live_web_context
        return await live_web_context(user_text, force=False)
    except Exception:
        return ""


def is_too_similar(candidate: str, previous_texts: Iterable[str]) -> tuple[bool, str]:
    candidate_signature = first_words_signature(candidate)
    for old in previous_texts:
        if not old:
            continue

        old_signature = first_words_signature(old)
        if candidate_signature and old_signature and candidate_signature == old_signature:
            return True, "совпадает начало ответа"

        seq = sequence_similarity(candidate, old)
        jac = jaccard_similarity(candidate, old)

        if seq >= 0.72:
            return True, f"слишком похожая формулировка ({seq:.2f})"

        if jac >= 0.55:
            return True, f"слишком похожий набор слов ({jac:.2f})"

    return False, ""


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def safe_short(text: str, max_len: int = 3500) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "…"


def safe_format(template: str, **kwargs) -> str:
    """Format prompt string safely, auto-escaping raw JSON braces if single."""
    try:
        return template.format(**kwargs)
    except KeyError:
        keys = set(kwargs.keys())
        temp = template
        for k in keys:
            temp = temp.replace(f"{{{k}}}", f"@@{k}@@")
        temp = temp.replace("{", "{{").replace("}", "}}")
        for k in keys:
            temp = temp.replace(f"@@{k}@@", f"{{{k}}}")
        return temp.format(**kwargs)


def clean_bot_reply(text: str) -> str:
    if not text:
        return ""
    text = text.strip()

    # Strip common reasoning / chain-of-thought blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:\w+)?\s*.*?\s*```", "", text, flags=re.DOTALL)

    # Aggressively remove leaked meta-reasoning (the main bug you are seeing)
    # These patterns appear when user pastes "Нужно:" planning text or model echoes instructions
    meta_patterns = [
        r"Нужно[:\s].*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"Важно[:\s].*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"Варианты[:\s].*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"Нужно отреагировать.*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"1\.\s*(Сыграть|Подколоть|Отреагировать|Начать с).*?(?=\n\n|\n[2-9]\.|\Z)",
        r"SEKSOV говорит.*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"wOnzA говорит.*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"Андрей Конфа.*?(?=\n\n|\n[А-ЯA-Z]|\Z)",
        r"^\s*\d+\.\s+(Подколоть|Сыграть|Отреагировать|Начать с|Просто).*",
    ]
    for pat in meta_patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE | re.DOTALL)

    # Remove any remaining "planning" style lines
    text = re.sub(r"^\s*(Нужно|Важно|Варианты|1\.|2\.|3\.|4\.)\s*.*$", "", text, flags=re.MULTILINE)

    # Final cleanup
    text = re.sub(r"^```(?:\w+)?", "", text)
    text = re.sub(r"```$", "", text)
    text = text.strip().strip('"').strip("'").strip()

    # If after cleaning almost nothing left or it still looks like meta, return empty so caller can fallback
    if len(text) < 5 or any(x in text.lower() for x in ["нужно отреагировать", "нужно:", "варианты:", "начать с seksov", "начать с wOnzA"]):
        return ""

    # Extra safety: if the reply still contains obvious planning markers after all stripping, nuke it
    if re.search(r"(Нужно|Важно|Варианты)\s*:", text, re.IGNORECASE):
        return ""

    return text
