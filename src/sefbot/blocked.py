"""Hard-block list for SefBot.

Users on this list cannot interact with the bot in any way (mentions, DMs,
prefix commands, slash commands, reaction feedback).

Persistence is a JSON file next to this module so:
  * a running bot picks up CLI changes without restart (mtime-cached reloads)
  * the `block` CLI does not need Discord tokens or AI keys

CLI (global after install):
    block access <user_id>
    block unblock <user_id>
    block list
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Optional, Set

_ROOT = Path(__file__).resolve().parent.parent.parent
BLOCKED_FILE = Path(
    os.getenv("SEFBOT_BLOCKED_FILE", str(_ROOT / "blocked_users.json"))
)

_cache_ids: Optional[Set[str]] = None
_cache_meta: Optional[Dict[str, dict]] = None
_cache_mtime: Optional[float] = None


def _empty() -> dict:
    return {"users": {}}


def _read_raw() -> dict:
    if not BLOCKED_FILE.exists():
        return _empty()
    try:
        data = json.loads(BLOCKED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    users = data.get("users")
    if not isinstance(users, dict):
        if isinstance(data.get("blocked"), list):
            users = {str(u): {"blocked_at": None, "reason": ""} for u in data["blocked"]}
        elif isinstance(data, list):
            users = {str(u): {"blocked_at": None, "reason": ""} for u in data}
        else:
            users = {}
        data = {"users": users}
    return data


def _write_raw(data: dict) -> None:
    tmp = BLOCKED_FILE.with_suffix(".json.tmp")
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, BLOCKED_FILE)
    global _cache_ids, _cache_meta, _cache_mtime
    _cache_ids = None
    _cache_meta = None
    _cache_mtime = None


def _load_cached() -> tuple[Set[str], Dict[str, dict]]:
    global _cache_ids, _cache_meta, _cache_mtime
    try:
        mtime = BLOCKED_FILE.stat().st_mtime
    except OSError:
        mtime = None
        data = _empty()
    else:
        if (
            _cache_ids is not None
            and _cache_meta is not None
            and _cache_mtime == mtime
        ):
            return _cache_ids, _cache_meta
        data = _read_raw()

    users = data.get("users") or {}
    ids: Set[str] = set()
    meta: Dict[str, dict] = {}
    for uid, info in users.items():
        uid = str(uid).strip()
        if not uid:
            continue
        ids.add(uid)
        meta[uid] = info if isinstance(info, dict) else {}
    _cache_ids = ids
    _cache_meta = meta
    _cache_mtime = mtime
    return ids, meta


def normalize_user_id(user_id) -> str:
    """Strip whitespace / mention wrappers; return digits-only id string."""
    raw = str(user_id or "").strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1]
        if raw.startswith("!"):
            raw = raw[1:]
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError(f"invalid Discord user id: {user_id!r}")
    return raw


def dynamic_blocked_ids() -> Set[str]:
    """User ids blocked via the CLI / blocked_users.json (not env/hardcoded)."""
    ids, _ = _load_cached()
    return set(ids)


def is_dynamically_blocked(user_id) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return uid in dynamic_blocked_ids()


def block_user(
    user_id,
    reason: str = "",
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
    """Add a user to the hard-block list or update existing block metadata.

    Returns True if newly blocked, False if updated existing block.
    """
    uid = normalize_user_id(user_id)
    data = _read_raw()
    users = data.setdefault("users", {})
    now = time.time()

    clean_reason = (reason or "").strip()
    clean_text = (offending_text or "").strip()
    if len(clean_text) > 3000:
        clean_text = clean_text[:3000] + "… [truncated]"

    history_event = {
        "timestamp": now,
        "reason": clean_reason,
        "category": category or "general",
        "offending_text": clean_text,
        "guild_id": str(guild_id or "").strip(),
        "guild_name": str(guild_name or "").strip(),
        "channel_id": str(channel_id or "").strip(),
        "trigger_source": trigger_source or "unknown",
        "strikes_detail": strikes_detail or "",
    }

    if uid in users:
        existing = users[uid] if isinstance(users[uid], dict) else {}
        if clean_reason:
            existing["reason"] = clean_reason
        if category:
            existing["category"] = category
        if clean_text:
            existing["offending_text"] = clean_text
        if channel_id:
            existing["channel_id"] = str(channel_id).strip()
        if guild_id:
            existing["guild_id"] = str(guild_id).strip()
        if guild_name:
            existing["guild_name"] = str(guild_name).strip()
        if user_tag:
            existing["user_tag"] = str(user_tag).strip()
        if trigger_source:
            existing["trigger_source"] = trigger_source
        if strikes_detail:
            existing["strikes_detail"] = strikes_detail

        existing["updated_at"] = now
        hist = existing.setdefault("history", [])
        if isinstance(hist, list):
            hist.append(history_event)
            existing["history"] = hist[-10:]
        users[uid] = existing
        _write_raw(data)
        return False

    users[uid] = {
        "blocked_at": now,
        "reason": clean_reason,
        "category": category or "general",
        "offending_text": clean_text,
        "channel_id": str(channel_id or "").strip(),
        "guild_id": str(guild_id or "").strip(),
        "guild_name": str(guild_name or "").strip(),
        "user_tag": str(user_tag or "").strip(),
        "trigger_source": trigger_source or "unknown",
        "strikes_detail": strikes_detail or "",
        "updated_at": now,
        "history": [history_event],
    }
    _write_raw(data)
    return True


def unblock_user(user_id) -> bool:
    """Remove a user from the hard-block list. Returns True if they were blocked."""
    uid = normalize_user_id(user_id)
    data = _read_raw()
    users = data.setdefault("users", {})
    if uid not in users:
        return False
    del users[uid]
    _write_raw(data)
    return True


def get_blocked_user(user_id) -> Optional[dict]:
    """Return detailed block metadata for user_id, or None if not dynamically blocked."""
    try:
        uid = normalize_user_id(user_id)
    except ValueError:
        return None
    _, meta = _load_cached()
    return meta.get(uid)


def list_blocked() -> Dict[str, dict]:
    """Return {user_id: meta} for dynamically blocked users."""
    _, meta = _load_cached()
    return dict(meta)

