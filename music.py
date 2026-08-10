"""Music: YouTube search → download → MP3 attached directly in chat.

`!music` / `/music` searches YouTube, downloads the audio with yt-dlp, converts
it to MP3 with ffmpeg, and attaches the file to the reply.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote_plus

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

MAX_DURATION_SEC = 12 * 60
MAX_FILE_BYTES = 24 * 1024 * 1024
AUDIO_BITRATE = "192"

_download_lock = asyncio.Lock()

_TMP_ROOT = Path(tempfile.gettempdir()) / "sefbot_music"
_SAFE_NAME = re.compile(r"[^\w\s\-\.\(\)\[\]]+", re.UNICODE)


def available() -> bool:
    """True if yt-dlp is importable (used for metadata search even without downloads)."""
    return yt_dlp is not None


def downloads_enabled() -> bool:
    """MP3 downloads are always on — the bot attaches the audio file directly."""
    return True


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _sanitize_filename(name: str) -> str:
    name = (name or "track").strip()
    name = _SAFE_NAME.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name[:80] or "track")


def _match_filter(info, *, incomplete=False):
    """Reject long videos before downloading."""
    duration = info.get("duration")
    if duration is not None and duration > MAX_DURATION_SEC:
        mins = MAX_DURATION_SEC // 60
        return f"track is too long ({int(duration) // 60} min; max {mins} min)"
    return None


def _download_sync(query: str, work_dir: Path) -> Tuple[Optional[Path], Optional[dict], Optional[str]]:
    """Blocking yt-dlp download. Returns (path, meta, error)."""
    if yt_dlp is None:
        return None, None, "yt-dlp is not installed (pip install yt-dlp)"
    if not ffmpeg_available():
        return None, None, "ffmpeg is not installed on this host — can't convert to mp3"

    query = (query or "").strip()
    if not query:
        return None, None, "give me a song name"
    if len(query) > 200:
        return None, None, "query is too long (max 200 chars)"

    work_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(work_dir / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "default_search": "ytsearch1",
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "match_filter": _match_filter,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb"]}},
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": AUDIO_BITRATE,
            }
        ],
        "prefer_ffmpeg": True,
    }

    url = f"ytsearch1:{query}"
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None, None, "couldn't find that track"
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None, None, f"no results for `{query}`"
                info = entries[0]
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).strip() or "download failed"
        if "too long" in msg.lower():
            return None, None, msg
        return None, None, f"download failed: {msg[:240]}"
    except Exception as e:
        return None, None, f"download failed: {type(e).__name__}: {str(e)[:200]}"

    video_id = info.get("id") or ""
    candidates = list(work_dir.glob(f"{video_id}*.mp3")) if video_id else []
    if not candidates:
        candidates = sorted(work_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None, "download finished but no mp3 turned up (ffmpeg ok?)"

    path = candidates[0]
    try:
        size = path.stat().st_size
    except OSError:
        return None, None, "couldn't read the downloaded file"
    if size <= 0:
        return None, None, "downloaded file is empty"
    if size > MAX_FILE_BYTES:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, None, (
            f"file is too big for Discord ({size // (1024 * 1024)} MiB; "
            f"max {MAX_FILE_BYTES // (1024 * 1024)} MiB)"
        )

    title = info.get("title") or query
    uploader = info.get("uploader") or info.get("channel") or "unknown"
    duration = info.get("duration")
    webpage = info.get("webpage_url") or info.get("original_url") or ""

    meta = {
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "url": webpage,
        "id": video_id,
        "filename": f"{_sanitize_filename(title)}.mp3",
        "bytes": size,
    }
    return path, meta, None


def _search_sync(query: str) -> Tuple[Optional[dict], Optional[str]]:
    """Search YouTube for a track without downloading. Returns (meta, error)."""
    query = (query or "").strip()
    if not query:
        return None, "give me a song name"
    if len(query) > 200:
        return None, "query is too long (max 200 chars)"

    if yt_dlp is None:
        return {
            "title": query,
            "uploader": "YouTube search",
            "duration": None,
            "url": f"https://www.youtube.com/results?search_query={quote_plus(query)}",
            "id": "",
            "search_only": True,
        }, None

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "extract_flat": False,
        "default_search": "ytsearch1",
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
            if not info:
                return None, "couldn't find that track"
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None, f"no results for `{query}`"
                info = entries[0]
    except Exception as e:
        return {
            "title": query,
            "uploader": "YouTube search",
            "duration": None,
            "url": f"https://www.youtube.com/results?search_query={quote_plus(query)}",
            "id": "",
            "search_only": True,
            "note": f"lookup fallback ({type(e).__name__})",
        }, None

    video_id = info.get("id") or ""
    webpage = (
        info.get("webpage_url")
        or info.get("original_url")
        or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    )
    if not webpage:
        webpage = f"https://www.youtube.com/results?search_query={quote_plus(query)}"

    return {
        "title": info.get("title") or query,
        "uploader": info.get("uploader") or info.get("channel") or "unknown",
        "duration": info.get("duration"),
        "url": webpage,
        "id": video_id,
        "search_only": False,
    }, None


async def search_song(query: str) -> Tuple[Optional[dict], Optional[str]]:
    """Async YouTube search — returns metadata + watch URL, never a file."""
    return await asyncio.to_thread(_search_sync, query)


async def download_song(query: str) -> Tuple[Optional[Path], Optional[dict], Optional[str]]:
    """Async wrapper: search + download to a unique temp dir.

    Caller must delete the returned path (and its parent dir) when done — use
    `cleanup(path)`.
    """
    stamp = f"{int(time.time() * 1000)}_{os.getpid()}"
    work_dir = _TMP_ROOT / stamp
    async with _download_lock:
        path, meta, err = await asyncio.to_thread(_download_sync, query, work_dir)
    if err:
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except OSError:
            pass
        return None, None, err
    return path, meta, None


def cleanup(path: Optional[Path]) -> None:
    """Remove the downloaded file and its work directory."""
    if path is None:
        return
    try:
        parent = path.parent
        if path.exists():
            path.unlink(missing_ok=True)
        if parent.parent == _TMP_ROOT and parent.exists():
            shutil.rmtree(parent, ignore_errors=True)
    except OSError:
        pass


def format_duration(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    m, sec = divmod(max(0, s), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"
