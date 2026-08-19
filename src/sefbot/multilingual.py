"""Multilingual routing.

Uses a lightweight language detector (langdetect — never the LLM, to save
cost) to spot non-English messages, and only when the message is in a
designated multilingual channel (``SEFBOT_MULTILINGUAL_CHANNELS``) routes the
reply to Llama 3.3 70B, which answers back in the user's language.
"""
import asyncio
import logging
from typing import Optional

from sefbot import config
from sefbot.services.llm_client import LLMError, llm

log = logging.getLogger("sefbot.multilingual")

try:
    import langdetect
    from langdetect import detect as _detect_raw

    langdetect.DetectorFactory.seed = 0
    _detect = _detect_raw
except Exception:
    _detect = None

_cache: dict[str, Optional[str]] = {}
_CACHE_MAX = 2048


async def detect_lang(text: str) -> Optional[str]:
    """Detect a message's ISO 639-1 language code (cached, runs off-loop)."""
    if _detect is None:
        return None
    key = (text or "").strip().lower()
    if not key or len(key) < 4:
        return "en"
    hit = _cache.get(key)
    if hit is not None:
        return hit
    loop = asyncio.get_running_loop()
    try:
        lang = await loop.run_in_executor(None, _detect, key)
    except Exception:
        return None
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    _cache[key] = lang
    return lang


async def translate_text(text: str, target_lang: str) -> str:
    """Translate text with the fast model before it hits the brain."""
    system = (
        f"You are a translation assistant. Translate the following text to "
        f"{target_lang} while preserving meaning and tone."
    )
    try:
        from sefbot import ai

        return await ai.chat(
            system,
            [{"role": "user", "content": text}],
            temperature=0.0,
            max_tokens=500,
            tier="fast",
        )
    except Exception:
        return text


async def maybe_multilingual_reply(
    channel, guild, text: str, lang: Optional[str]
) -> Optional[str]:
    """Return a Llama 3.3 70B reply in the message's language, or None.

    Only fires inside a designated multilingual channel for non-English text.
    """
    if not lang or lang == "en":
        return None
    if guild is None or channel is None:
        return None
    if str(channel.id) not in config.MULTILINGUAL_CHANNELS:
        return None
    if not config.GROQ_API_KEY:
        return None
    system = (
        "You are a friendly multilingual Discord assistant. Reply in the SAME "
        "language the user wrote in. Keep it natural, concise and helpful. "
        "No disclaimers, no 'as an AI' talk, no emoji."
    )
    try:
        return await llm.chat(
            config.MULTILINGUAL_MODEL,
            [{"role": "user", "content": text[:1500]}],
            system=system,
            temperature=0.6,
            max_tokens=600,
            base_url=config.GROQ_BASE_URL,
            api_key=config.GROQ_API_KEY,
        )
    except LLMError as e:
        log.warning("multilingual reply failed: %s", e)
        return None
