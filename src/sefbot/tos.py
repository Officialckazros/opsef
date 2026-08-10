"""OpSef Terms of Service — acceptance gate + violation detection.

Canonical page:
  https://wearegays.net/opsef-tos.html

Users must `!tos accept` (current version) before normal bot use.
Clear ToS violations hard-block the Discord user id via blocked.py.
"""
from __future__ import annotations

import re
import time
from typing import Optional, Tuple

from sefbot import config
from sefbot import db

TOS_VERSION = "1.0"
TOS_URL = "https://wearegays.net/opsef-tos.html"
PRIVACY_URL = "https://wearegays.net/opsef-privacy.html"

_LEAK_STRIKE_LIMIT = 3
_SPAM_WINDOW_SEC = 20.0
_SPAM_MAX = 12

TOS_ALLOWED_COMMANDS = frozenset({
    "tos", "terms", "termsofservice",
    "privacy", "privacypolicy", "pp",
    "help", "about",
    "dmblock", "dmunblock", "mydm",
})


_MINOR_SEX_RE = re.compile(
    r"(?is)(?:"
    r"(?:child|children|kid|kids|toddler|infant|minor|minors|underage|pre-?teen|preteens?|"
    r"loli|lolita|shota|shotacon|lolicon|"
    r"(?:1[0-7])\s*(?:yo|y/o|year[- ]olds?))"
    r".{0,40}"
    r"(?:sex|sexual|porn|nude|naked|rape|molest|erotic|nsfw|hentai|csam|\bcp\b)"
    r"|"
    r"(?:sex|sexual|porn|nude|naked|rape|molest|erotic|nsfw|hentai|csam|\bcp\b)"
    r".{0,40}"
    r"(?:child|children|kid|kids|toddler|infant|minor|minors|underage|pre-?teen|"
    r"loli|lolita|shota|shotacon|lolicon|(?:1[0-7])\s*(?:yo|y/o|year[- ]olds?))"
    r")"
)

_DOXX_RE = re.compile(
    r"(?is)(?:"
    r"\bdoxx?(?:ing|es|ed)?\b|"
    r"\bswatt?ing\b|"
    r"(?:drop|leak|post|publish|doxx?)\s+(?:their|his|her|someone'?s?)\s+"
    r"(?:address|phone|ssn|social\s*security|real\s*name|home)|"
    r"(?:find|get|give)\s+me\s+(?:their|his|her)\s+(?:home\s*)?address|"
    r"social\s*security\s*number|"
    r"\b\d{3}-\d{2}-\d{4}\b"
    r")"
)

_CRED_THEFT_RE = re.compile(
    r"(?is)(?:"
    r"(?:steal|grab|phish|harvest)\s+(?:discord\s+)?(?:tokens?|passwords?|sessions?|cookies?)|"
    r"(?:discord\s+)?token\s*(?:logger|grabber|stealer)|"
    r"nitro\s*scam\s*link|"
    r"(?:free\s+)?nitro\s+from\s+this\s+link|"
    r"paste\s+(?:your|the)\s+token|"
    r"webhook\s*spammer\s*for\s+raiding"
    r")"
)

_MALWARE_RE = re.compile(
    r"(?is)(?:"
    r"(?:rat\s*stub|remote\s*access\s*trojan)\s+for\s+(?:victims?|discord)|"
    r"undetectable\s+(?:stealer|rat)\s+build|"
    r"spread\s+(?:this\s+)?(?:malware|virus|trojan)\s+on\s+discord"
    r")"
)

_SPAM_RE = re.compile(r"(.)\1{40,}")


def _uid(user_id) -> str:
    return str(user_id or "").strip()


def has_accepted(user_id) -> bool:
    """True if user accepted the current ToS version."""
    uid = _uid(user_id)
    if not uid:
        return False
    if config.is_bot_owner(uid):
        return True
    return (db.user_flag_get(uid, "tos_accepted") or "") == TOS_VERSION


def accept(user_id) -> None:
    uid = _uid(user_id)
    db.user_flag_set(uid, "tos_accepted", TOS_VERSION)
    db.user_flag_set(uid, "tos_accepted_at", str(time.time()))


def reject(user_id) -> None:
    uid = _uid(user_id)
    db.user_flag_set(uid, "tos_accepted", "")
    db.user_flag_set(uid, "tos_rejected_at", str(time.time()))


def status_line(user_id) -> str:
    if has_accepted(user_id):
        when = db.user_flag_get(_uid(user_id), "tos_accepted_at") or ""
        try:
            ts = float(when)
            when_s = time.strftime("%Y-%m-%d", time.gmtime(ts))
        except (TypeError, ValueError):
            when_s = "unknown date"
        return f"accepted **v{TOS_VERSION}** ({when_s})"
    return f"**not accepted** — required version **v{TOS_VERSION}**"


def need_accept_message(prefix: str = "!") -> str:
    return (
        f"**Terms of Service required**\n"
        f"Read: {TOS_URL}\n"
        f"Privacy: {PRIVACY_URL}\n\n"
        f"If you agree, run `{prefix}tos accept`.\n"
        f"No chat or other commands until you accept **v{TOS_VERSION}**."
    )


def command_allowed_without_tos(cmd_name: str) -> bool:
    return (cmd_name or "").strip().lower() in TOS_ALLOWED_COMMANDS


def detect_hard_violation_info(text: str) -> Optional[Tuple[str, str]]:
    """Return (reason, category) if text is a clear ToS violation, else None."""
    if not text or len(text.strip()) < 4:
        return None
    t = text.strip()
    if _MINOR_SEX_RE.search(t):
        return "sexual content involving minors", "minor_sex_csam"
    if _DOXX_RE.search(t):
        return "doxxing / private personal data abuse", "doxxing"
    if _CRED_THEFT_RE.search(t):
        return "credential / token theft or phishing", "credential_theft"
    if _MALWARE_RE.search(t):
        return "malware distribution / abuse tooling", "malware"
    return None


def detect_hard_violation(text: str) -> Optional[str]:
    """Return a short reason if text is a clear absolute ToS violation."""
    res = detect_hard_violation_info(text)
    return res[0] if res else None


def _strike(user_id: str, key: str) -> int:
    n = db.user_flag_int(user_id, key, 0) + 1
    db.user_flag_set(user_id, key, str(n))
    return n


def note_leak_attempt(user_id) -> Tuple[bool, int]:
    """Count a prompt-leak attempt. Returns (should_block, strike_count)."""
    uid = _uid(user_id)
    if config.is_bot_owner(uid):
        return False, 0
    n = _strike(uid, "tos_leak_strikes")
    return n >= _LEAK_STRIKE_LIMIT, n


def note_message_for_spam(user_id) -> bool:
    """Track rapid-fire messages; True if over spam threshold (should block)."""
    uid = _uid(user_id)
    if config.is_bot_owner(uid):
        return False
    now = time.time()
    raw = db.user_flag_get(uid, "tos_spam_bucket") or ""
    times = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            t = float(part)
        except ValueError:
            continue
        if now - t <= _SPAM_WINDOW_SEC:
            times.append(t)
    times.append(now)
    db.user_flag_set(uid, "tos_spam_bucket", ",".join(f"{t:.3f}" for t in times[-30:]))
    return len(times) > _SPAM_MAX


def _infer_category(reason: str, explicit_category: str = "") -> str:
    if explicit_category:
        return explicit_category
    r = (reason or "").lower()
    if "minor" in r or "csam" in r or "child" in r:
        return "minor_sex_csam"
    if "doxx" in r or "private" in r or "personal data" in r:
        return "doxxing"
    if "credential" in r or "token" in r or "phish" in r:
        return "credential_theft"
    if "malware" in r or "trojan" in r or "rat" in r:
        return "malware"
    if "leak" in r or "prompt" in r or "exfiltration" in r:
        return "prompt_leak"
    if "spam" in r or "flood" in r:
        return "spam_flood"
    if "model" in r or "policy" in r:
        return "model_policy_flag"
    return "general_tos_violation"


def hard_block(
    user_id,
    reason: str,
    *,
    category: str = "",
    offending_text: str = "",
    channel_id: str = "",
    guild_id: str = "",
    guild_name: str = "",
    user_tag: str = "",
    trigger_source: str = "",
    strikes_detail: str = "",
) -> bool:
    """Persist a ToS hard-block with rich violation metadata. Returns True if newly blocked."""
    uid = _uid(user_id)
    if not uid or config.is_bot_owner(uid):
        return False

    tos_reason = reason if reason.lower().startswith("tos:") else f"tos: {reason}"
    cat = _infer_category(reason, category)

    db.user_flag_set(uid, "tos_emergency_block", "1")

    try:
        from sefbot import blocked
        return blocked.block_user(
            uid,
            reason=tos_reason[:250],
            category=cat,
            offending_text=offending_text,
            channel_id=channel_id,
            guild_id=guild_id,
            guild_name=guild_name,
            user_tag=user_tag,
            trigger_source=trigger_source,
            strikes_detail=strikes_detail,
        )
    except Exception as e:
        print(f"[tos] block failed for {uid}: {e}")
        return True


def is_emergency_blocked(user_id) -> bool:
    return db.user_flag_get(_uid(user_id), "tos_emergency_block") == "1"


def check_message(user_id, text: str) -> Optional[str]:
    """
    Run ToS detectors on a user message.

    Returns a block reason if the user should be hard-blocked now, else None.
    Does not itself perform the block (caller should hard_block + reply).
    """
    uid = _uid(user_id)
    if not uid or config.is_bot_owner(uid):
        return None

    info = detect_hard_violation_info(text or "")
    if info:
        return info[0]

    if text and _SPAM_RE.search(text):
        n = _strike(uid, "tos_spam_strikes")
        if n >= 5:
            return "repeated spam content"

    if note_message_for_spam(uid):
        return "spam / abuse flood"

    return None


def handle_model_tos_flag(user_id, flag) -> Optional[str]:
    """
    Model may emit tos_violation: {\"reason\": \"...\"} or a plain string.
    Returns block reason if actionable.
    """
    if not flag:
        return None
    if config.is_bot_owner(user_id):
        return None
    if isinstance(flag, dict):
        reason = str(flag.get("reason") or flag.get("type") or flag.get("violation") or "").strip()
        severity = str(flag.get("severity") or "high").lower()
    else:
        reason = str(flag).strip()
        severity = "high"
    if not reason or len(reason) < 3:
        return None
    if severity in ("low", "medium", "warn"):
        n = _strike(_uid(user_id), "tos_model_strikes")
        if n >= 3:
            return f"repeated policy abuse ({reason[:80]})"
        return None
    return reason[:120]


def page_footer() -> str:
    return f"ToS v{TOS_VERSION}: {TOS_URL}"

