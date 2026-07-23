"""
Live web retrieval for PredskazBot.

Replaces the old ad-hoc fetch_live_web_info() kludge with:
  - intent detection (weather / fx+crypto / general facts)
  - small provider functions with clear contracts
  - DuckDuckGo Instant Answer JSON API (not HTML scrape)
  - structured snippets joined for LLM context

No required heavy deps beyond httpx (already pulled by openai/telegram).
All network failures are soft — empty string on total miss.
"""
from __future__ import annotations

import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

logger = logging.getLogger("predskazbot.web")

# --- config (env-overridable) -------------------------------------------------

DEFAULT_TIMEOUT = float(os.getenv("WEB_SEARCH_TIMEOUT", "5.0"))
DEFAULT_CITY = os.getenv("WEB_SEARCH_DEFAULT_CITY", "Moscow")
MAX_SNIPPETS = int(os.getenv("WEB_SEARCH_MAX_SNIPPETS", "4"))
MAX_CHARS = int(os.getenv("WEB_SEARCH_MAX_CHARS", "1800"))
USER_AGENT = os.getenv(
    "WEB_SEARCH_USER_AGENT",
    "PredskazBot/3.5 (+https://github.com/AdamFolz/predskazbot; live-context)",
)

# RU city surface forms → wttr.in query
_CITY_ALIASES: dict[str, str] = {
    "москве": "Moscow",
    "москва": "Moscow",
    "мск": "Moscow",
    "moscow": "Moscow",
    "питере": "Saint Petersburg",
    "питера": "Saint Petersburg",
    "питер": "Saint Petersburg",
    "спб": "Saint Petersburg",
    "петербург": "Saint Petersburg",
    "петербурге": "Saint Petersburg",
    "ленинграде": "Saint Petersburg",
    "туле": "Tula",
    "тула": "Tula",
    "казани": "Kazan",
    "казань": "Kazan",
    "екатеринбурге": "Yekaterinburg",
    "екб": "Yekaterinburg",
    "новосибирске": "Novosibirsk",
    "новосибирск": "Novosibirsk",
    "сочи": "Sochi",
    "краснодаре": "Krasnodar",
    "краснодар": "Krasnodar",
    "самара": "Samara",
    "самаре": "Samara",
    "нижнем": "Nizhny Novgorod",
    "нижегород": "Nizhny Novgorod",
    "воронеже": "Voronezh",
    "воронеж": "Voronezh",
    "ростове": "Rostov-on-Don",
    "минске": "Minsk",
    "минск": "Minsk",
    "киеве": "Kyiv",
    "киев": "Kyiv",
    "алматы": "Almaty",
    "ташкенте": "Tashkent",
    "ташкент": "Tashkent",
}


class Intent(str, Enum):
    WEATHER = "weather"
    MARKET = "market"  # crypto + fiat
    FACT = "fact"  # general knowledge / news-ish
    NONE = "none"


@dataclass(frozen=True, slots=True)
class WebSnippet:
    source: str
    text: str

    def render(self) -> str:
        return f"[{self.source}] {self.text}"


# --- intent detection ---------------------------------------------------------

_WEATHER_RE = re.compile(
    r"(?:"
    r"погод\w*|температур\w*|прогноз\s+погоды|"
    r"дожд\w*|снег\w*|ветер\w*|градус\w*|"
    r"weather|forecast"
    r")",
    re.IGNORECASE,
)
_WEATHER_CITY_RE = re.compile(
    r"(?:погод\w*|температур\w*|прогноз\s+погоды|weather|forecast)"
    r"\s+(?:в\s+|на\s+|для\s+)?([а-яёa-z][а-яёa-z\-]{1,32})",
    re.IGNORECASE,
)

_MARKET_RE = re.compile(
    r"(?:"
    r"курс\w*|доллар\w*|евро|юан\w*|йен\w*|"
    r"биткоин\w*|биток\w*|эфир\w*|ethereum|bitcoin|"
    r"\busd\b|\beur\b|\bbtc\b|\beth\b|\brus\b|\brub\b|"
    r"крипт\w*|exchange\s*rate"
    r")",
    re.IGNORECASE,
)

_FACT_PREFIXES = (
    "что такое",
    "что это",
    "кто такой",
    "кто такая",
    "кто такие",
    "как сделать",
    "как приготовить",
    "как доехать",
    "как работает",
    "сколько стоит",
    "когда будет",
    "где находится",
    "зачем нужен",
    "почему",
    "рецепт",
    "новости",
    "что значит",
    "what is",
    "who is",
    "how to",
    "when is",
    "where is",
)


def detect_intents(text: str) -> set[Intent]:
    """Return zero or more intents implied by the user text."""
    if not text or not text.strip():
        return set()
    lowered = text.lower().replace("ё", "е")
    found: set[Intent] = set()

    if _WEATHER_RE.search(lowered):
        found.add(Intent.WEATHER)
    if _MARKET_RE.search(lowered):
        found.add(Intent.MARKET)

    stripped = lowered.strip()
    for p in _FACT_PREFIXES:
        if stripped.startswith(p) or f" {p} " in f" {stripped} ":
            found.add(Intent.FACT)
            break
    # bare question mark + short question → treat as fact-ish
    if "?" in text and len(text) <= 160 and Intent.FACT not in found:
        if any(w in stripped for w in ("что", "кто", "как", "где", "когда", "сколько", "what", "who", "how", "where", "when")):
            found.add(Intent.FACT)

    return found or {Intent.NONE}


def needs_live_web(text: str) -> bool:
    """True when live retrieval is worth the latency."""
    intents = detect_intents(text)
    return bool(intents - {Intent.NONE})


# --- helpers ------------------------------------------------------------------

def _normalize_city(raw: str | None) -> str:
    if not raw:
        return DEFAULT_CITY
    key = raw.strip().lower().replace("ё", "е").strip(".,!?")
    if key in _CITY_ALIASES:
        return _CITY_ALIASES[key]
    # strip common case endings roughly: Москве → москв…
    for alias, city in _CITY_ALIASES.items():
        if key.startswith(alias[:4]) and len(key) <= len(alias) + 2:
            return city
    # fallback: title-case latin / leave cyrillic for wttr
    return raw.strip().title() if re.match(r"^[A-Za-z\-]+$", raw.strip()) else raw.strip()


def _extract_weather_city(text: str) -> str:
    m = _WEATHER_CITY_RE.search(text.lower().replace("ё", "е"))
    if m:
        return _normalize_city(m.group(1))
    return DEFAULT_CITY


async def _client():
    import httpx

    return httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/plain,*/*"},
        follow_redirects=True,
    )


def _clip(text: str, n: int = 400) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rsplit(" ", 1)[0] + "…"


# --- providers ----------------------------------------------------------------

async def fetch_weather(city: str | None = None) -> WebSnippet | None:
    city_q = _normalize_city(city) if city else DEFAULT_CITY
    url = (
        f"https://wttr.in/{urllib.parse.quote(city_q)}"
        f"?M&lang=ru&format=%l:+%c+%t+(ощущается+%f),+ветер+%w,+влажность+%h"
    )
    try:
        async with await _client() as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        body = r.text.strip()
        if not body or "Unknown location" in body or len(body) > 200:
            return None
        return WebSnippet("weather/wttr.in", f"Погода ({city_q}): {body}")
    except Exception as exc:
        logger.debug("weather failed city=%s: %s", city_q, exc)
        return None


async def fetch_markets() -> list[WebSnippet]:
    out: list[WebSnippet] = []
    # Crypto via CoinGecko (no key)
    try:
        async with await _client() as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bitcoin,ethereum,toncoin",
                    "vs_currencies": "usd,rub",
                },
            )
        if r.status_code == 200:
            data = r.json()
            parts = []
            for coin, label in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("toncoin", "TON")):
                row = data.get(coin) or {}
                usd, rub = row.get("usd"), row.get("rub")
                if usd is not None:
                    if rub is not None:
                        parts.append(f"{label}: ${usd:,.0f} / {rub:,.0f} ₽")
                    else:
                        parts.append(f"{label}: ${usd:,.0f}")
            if parts:
                out.append(WebSnippet("market/coingecko", "Крипто: " + "; ".join(parts)))
    except Exception as exc:
        logger.debug("coingecko failed: %s", exc)

    # USD/EUR via open.er-api.com (free, no key)
    try:
        async with await _client() as client:
            r = await client.get("https://open.er-api.com/v6/latest/USD")
        if r.status_code == 200:
            data = r.json()
            rates = data.get("rates") or {}
            rub = rates.get("RUB")
            eur = rates.get("EUR")
            bits = []
            if rub:
                bits.append(f"USD/RUB {rub:.2f}")
            if eur and rub:
                bits.append(f"EUR/RUB {rub / eur:.2f}")
            elif eur:
                bits.append(f"EUR/USD {1 / eur:.4f}" if eur else "")
            if bits:
                out.append(WebSnippet("market/er-api", "Форекс: " + ", ".join(b for b in bits if b)))
    except Exception as exc:
        logger.debug("er-api failed: %s", exc)

    return out


async def fetch_facts(query: str, limit: int = 3) -> list[WebSnippet]:
    """DuckDuckGo Instant Answer API — JSON, no HTML scrape."""
    q = (query or "").strip()
    if not q:
        return []
    url = "https://api.duckduckgo.com/"
    params = {
        "q": q[:200],
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
        "t": "predskazbot",
    }
    out: list[WebSnippet] = []
    try:
        async with await _client() as client:
            r = await client.get(url, params=params)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as exc:
        logger.debug("ddg instant failed: %s", exc)
        return []

    abstract = (data.get("AbstractText") or "").strip()
    abstract_src = (data.get("AbstractSource") or "DuckDuckGo").strip()
    if abstract:
        out.append(WebSnippet(f"fact/{abstract_src}", _clip(abstract, 500)))

    answer = (data.get("Answer") or "").strip()
    if answer and answer != abstract:
        out.append(WebSnippet("fact/answer", _clip(answer, 300)))

    # Related topics / definition-like
    for item in data.get("RelatedTopics") or []:
        if len(out) >= limit:
            break
        if isinstance(item, dict) and item.get("Text"):
            out.append(WebSnippet("fact/related", _clip(str(item["Text"]), 280)))
        elif isinstance(item, dict) and item.get("Topics"):
            for sub in item["Topics"][:2]:
                if len(out) >= limit:
                    break
                if isinstance(sub, dict) and sub.get("Text"):
                    out.append(WebSnippet("fact/related", _clip(str(sub["Text"]), 280)))

    # Definition block
    definition = (data.get("Definition") or "").strip()
    if definition and len(out) < limit:
        def_src = (data.get("DefinitionSource") or "definition").strip()
        out.append(WebSnippet(f"fact/{def_src}", _clip(definition, 400)))

    return out[:limit]


# --- public API ---------------------------------------------------------------

def format_snippets(snippets: Iterable[WebSnippet], max_chars: int = MAX_CHARS) -> str:
    lines: list[str] = []
    total = 0
    for snip in snippets:
        line = snip.render()
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


async def live_web_context(user_text: str, *, force: bool = False) -> str:
    """
    Main entry: build a short multi-source context block for the LLM.

    force=True skips intent gate (tool path / explicit /ask enrichment).
    """
    text = (user_text or "").strip()
    if not text:
        return ""

    intents = detect_intents(text)
    if not force and intents == {Intent.NONE}:
        return ""
    if force and intents == {Intent.NONE}:
        # explicit tool call with free-form query → treat as fact search
        intents = {Intent.FACT}

    collected: list[WebSnippet] = []

    if Intent.WEATHER in intents:
        city = _extract_weather_city(text)
        snip = await fetch_weather(city)
        if snip:
            collected.append(snip)

    if Intent.MARKET in intents:
        collected.extend(await fetch_markets())

    if Intent.FACT in intents or (force and not collected):
        # strip bot-address junk for cleaner DDG query
        q = re.sub(
            r"@\w+|хуебот|еблобот|hyebot|сексялка|предсказалка|ботик|оракул",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        q = re.sub(r"\s+", " ", q).strip() or text
        collected.extend(await fetch_facts(q, limit=MAX_SNIPPETS))

    if not collected:
        return ""
    return format_snippets(collected[:MAX_SNIPPETS], max_chars=MAX_CHARS)


# Back-compat name used across bot.py / older call sites
async def fetch_live_web_info(user_text: str) -> str:
    return await live_web_context(user_text, force=False)
