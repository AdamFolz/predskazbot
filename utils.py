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
    """Lightweight live internet retrieval for household/practical queries."""
    if not user_text:
        return ""
    lowered = user_text.lower()
    snippets: list[str] = []

    try:
        import httpx
        async with httpx.AsyncClient(timeout=4.5) as client:
            # 1. Weather lookup
            weather_match = re.search(
                r"(?:погод[а-я]*|температур[а-я]*|дождь|снег|прогноз\s+погоды)\s+(?:в\s+|на\s+)?([а-яА-Яa-zA-Z-]+)",
                lowered,
            )
            if weather_match or "погод" in lowered:
                city = weather_match.group(1).title() if weather_match else "Moscow"
                city_clean = city.replace("Москве", "Moscow").replace("Питере", "Saint Petersburg").replace("Туле", "Tula")
                try:
                    r = await client.get(f"http://wttr.in/{city_clean}?M&format=%l:+%c+%t+(ощущается+как+%f),+ветер+%w,+влажность+%h")
                    if r.status_code == 200 and "Unknown" not in r.text and len(r.text.strip()) < 150:
                        snippets.append(f"Погода ({city_clean}): {r.text.strip()}")
                except Exception:
                    pass

            # 2. Currency/exchange rate lookup
            if any(w in lowered for w in ["курс", "доллар", "евро", "биток", "биткоин", "usd", "btc", "rub"]):
                try:
                    r = await client.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd,rub")
                    if r.status_code == 200:
                        data = r.json()
                        btc_usd = data.get("bitcoin", {}).get("usd", 0)
                        btc_rub = data.get("bitcoin", {}).get("rub", 0)
                        if btc_usd:
                            snippets.append(f"Курс Bitcoin (BTC): ${btc_usd:,.0f} / {btc_rub:,.0f} RUB")
                except Exception:
                    pass

            # 3. DuckDuckGo quick factual search if asked practical/factual questions
            if any(lowered.startswith(p) or f" {p} " in lowered for p in ["что такое", "кто такой", "как сделать", "рецепт", "новости", "почему", "когда будет", "сколько стоит", "как доехать"]):
                try:
                    import urllib.parse
                    query = urllib.parse.quote(user_text[:100])
                    r = await client.get(
                        f"https://html.duckduckgo.com/html/?q={query}",
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    )
                    if r.status_code == 200:
                        found = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', r.text, re.DOTALL)
                        clean_found = []
                        for s in found[:2]:
                            clean_s = re.sub(r'<[^>]+>', '', s).strip()
                            if clean_s:
                                clean_found.append(clean_s)
                        if clean_found:
                            snippets.append("Справка из интернета: " + " | ".join(clean_found))
                except Exception:
                    pass
    except Exception:
        pass

    return "\n".join(snippets)


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
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Some non-OpenAI models (GLM/LLaMA) emit their own tool-call syntax as
    # plain text content instead of populating message.tool_calls. Strip any
    # such leakage so it never reaches the user, whether closed or dangling.
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*", "", text, flags=re.DOTALL)
    text = text.strip()
    text = re.sub(r"^```(?:\w+)?", "", text)
    text = re.sub(r"```$", "", text)
    text = text.strip().strip('"').strip()
    return text
