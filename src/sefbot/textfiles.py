"""Text file handling: read, decode, and extract .txt file attachments."""

from __future__ import annotations

import logging
from typing import List, Optional

import discord

from sefbot import config

log = logging.getLogger("sefbot.textfiles")

_TEXT_EXT = (".txt", ".text", ".log", ".csv")


def is_text_attachment(attachment: Optional[discord.Attachment]) -> bool:
    """Return True if the attachment appears to be a plain text file."""
    if attachment is None:
        return False
    name = (getattr(attachment, "filename", None) or "").lower()
    ct = (getattr(attachment, "content_type", None) or "").lower()
    if any(name.endswith(ext) for ext in _TEXT_EXT):
        return True
    if ct.startswith("text/plain") or ct.startswith("text/csv") or ct.startswith("text/x-log"):
        return True
    return False


async def read_attachment_text(
    attachment: discord.Attachment,
    max_chars: int = 50_000,
    max_bytes: int = config.IMPORT_MAX_BYTES,
) -> Optional[str]:
    """Asynchronously read and decode a text attachment safely with truncation."""
    if attachment is None:
        return None
    fname = getattr(attachment, "filename", "attachment.txt")
    size = getattr(attachment, "size", 0)
    if size > max_bytes:
        return (
            f"[attached text file: {fname} — omitted because size "
            f"({size:,} bytes) exceeds configured limit ({max_bytes:,} bytes)]"
        )
    try:
        data = await attachment.read()
    except Exception as e:
        log.warning("failed to read attachment %s: %s", fname, e)
        return f"[attached text file: {fname} — error reading file: {e}]"

    try:
        raw_text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            raw_text = data.decode("latin-1")
        except Exception:
            raw_text = data.decode("utf-8", errors="replace")

    raw_text = raw_text.replace("\x00", "")
    if len(raw_text) > max_chars:
        omitted = len(raw_text) - max_chars
        raw_text = (
            raw_text[:max_chars].rstrip()
            + f"\n\n[... truncated ({omitted:,} characters omitted) ...]"
        )

    return f"[attached text file: {fname}]\n{raw_text}"


async def extract_message_text_files(
    message: Optional[discord.Message],
    max_files: int = 5,
    max_chars_per_file: int = 50_000,
) -> str:
    """Extract and decode all .txt file attachments on a message or its replied parent."""
    if message is None:
        return ""

    text_attachments: List[discord.Attachment] = [
        a for a in (getattr(message, "attachments", None) or []) if is_text_attachment(a)
    ]

    if not text_attachments and getattr(message, "reference", None):
        ref = message.reference
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and getattr(resolved, "attachments", None):
            text_attachments = [a for a in resolved.attachments if is_text_attachment(a)]
        elif (
            getattr(ref, "message_id", None)
            and hasattr(message, "channel")
            and hasattr(message.channel, "fetch_message")
        ):
            try:
                parent = await message.channel.fetch_message(ref.message_id)
                if parent and parent.attachments:
                    text_attachments = [
                        a for a in parent.attachments if is_text_attachment(a)
                    ]
            except Exception as e:
                log.debug("could not fetch referenced message %s: %s", ref.message_id, e)

    if not text_attachments:
        return ""

    blocks: List[str] = []
    for a in text_attachments[:max_files]:
        block = await read_attachment_text(a, max_chars=max_chars_per_file)
        if block:
            blocks.append(block)

    return "\n\n".join(blocks)
