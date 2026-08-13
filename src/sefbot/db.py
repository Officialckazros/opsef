"""SQLite persistence — the bot's growing brain.

Tables
------
memories      : facts about a subject (user id or 'server')
lessons       : behavioral guidance from feedback
feedback      : raw up/down + corrections
commands      : community prompt-defined commands
interactions  : stats + skill level
kv            : misc key/value (mood, lurk timers, etc.)
relationships : per-user bond score, nickname, grudge
conversations : short-term user↔bot turns
quotes        : hall of shame / saved lines
guild_settings: per-server config (persona, lurk, etc.)
"""
import json
import re
import sqlite3
import time
from typing import List, Optional

from sefbot import config

_conn: Optional[sqlite3.Connection] = None

_gs_cache: dict = {}
_GS_TTL = 5.0
_lessons_cache: Optional[list] = None
_lessons_ts: float = 0.0
_LESSONS_TTL = 30.0
_mem_cache: dict = {}
_MEM_TTL = 15.0
_MEM_CACHE_MAX = 256


def _mem_cache_set(key, rows) -> None:
    if len(_mem_cache) >= _MEM_CACHE_MAX:
        _mem_cache.clear()
    _mem_cache[key] = (time.time(), rows)


def _mem_cache_get(key):
    hit = _mem_cache.get(key)
    if hit is not None and time.time() - hit[0] < _MEM_TTL:
        return hit[1]
    return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL DEFAULT 'server',
    content    TEXT NOT NULL,
    author     TEXT,
    guild_id   TEXT,
    importance REAL DEFAULT 0.5,
    created    REAL NOT NULL,
    updated    REAL
);
CREATE TABLE IF NOT EXISTS lessons (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    content   TEXT NOT NULL UNIQUE,
    source    TEXT,
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_msg   TEXT,
    bot_msg    TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    note       TEXT,
    author     TEXT,
    processed  INTEGER DEFAULT 0,
    created    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    behavior    TEXT NOT NULL,
    author      TEXT,
    guild_id    TEXT,
    uses        INTEGER DEFAULT 0,
    created     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS interactions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    author   TEXT,
    guild_id TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS relationships (
    user_id    TEXT NOT NULL,
    guild_id   TEXT NOT NULL,
    score      REAL NOT NULL DEFAULT 0.0,
    nickname   TEXT,
    grudge     TEXT,
    bond_label TEXT DEFAULT 'stranger',
    updated    REAL NOT NULL,
    PRIMARY KEY (user_id, guild_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    text     TEXT NOT NULL,
    about    TEXT,
    author   TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    data     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS server_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT UNIQUE,
    guild_id         TEXT NOT NULL,
    guild_name       TEXT,
    channel_id       TEXT NOT NULL,
    channel_name     TEXT,
    user_id          TEXT NOT NULL,
    username         TEXT NOT NULL,
    display_name     TEXT,
    content          TEXT NOT NULL,
    has_bad_words    INTEGER DEFAULT 0,
    bad_words_found  TEXT,
    created          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject, guild_id);
CREATE INDEX IF NOT EXISTS idx_convo_user ON conversations(user_id, guild_id, created);
CREATE INDEX IF NOT EXISTS idx_quotes_guild ON quotes(guild_id);
CREATE INDEX IF NOT EXISTS idx_msg_guild_user ON server_messages(guild_id, user_id, created);
CREATE INDEX IF NOT EXISTS idx_msg_user ON server_messages(user_id, created);
CREATE INDEX IF NOT EXISTS idx_msg_bad ON server_messages(guild_id, has_bad_words);
"""

_WORD = re.compile(r"[a-z0-9]{3,}")


def _migrate(c: sqlite3.Connection) -> None:
    """Additive migrations so an already-deployed DB upgrades in place."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(memories)").fetchall()}
    if "subject" not in cols:
        c.execute("ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT 'server'")
    if "importance" not in cols:
        c.execute("ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 0.5")
    if "updated" not in cols:
        c.execute("ALTER TABLE memories ADD COLUMN updated REAL")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS relationships (
        user_id    TEXT NOT NULL,
        guild_id   TEXT NOT NULL,
        score      REAL NOT NULL DEFAULT 0.0,
        nickname   TEXT,
        grudge     TEXT,
        bond_label TEXT DEFAULT 'stranger',
        updated    REAL NOT NULL,
        PRIMARY KEY (user_id, guild_id)
    );
    CREATE TABLE IF NOT EXISTS conversations (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        role     TEXT NOT NULL,
        content  TEXT NOT NULL,
        created  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS quotes (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id TEXT NOT NULL,
        text     TEXT NOT NULL,
        about    TEXT,
        author   TEXT,
        created  REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        data     TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS server_messages (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id       TEXT UNIQUE,
        guild_id         TEXT NOT NULL,
        guild_name       TEXT,
        channel_id       TEXT NOT NULL,
        channel_name     TEXT,
        user_id          TEXT NOT NULL,
        username         TEXT NOT NULL,
        display_name     TEXT,
        content          TEXT NOT NULL,
        has_bad_words    INTEGER DEFAULT 0,
        bad_words_found  TEXT,
        created          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject, guild_id);
    CREATE INDEX IF NOT EXISTS idx_convo_user ON conversations(user_id, guild_id, created);
    CREATE INDEX IF NOT EXISTS idx_quotes_guild ON quotes(guild_id);
    CREATE INDEX IF NOT EXISTS idx_msg_guild_user ON server_messages(guild_id, user_id, created);
    CREATE INDEX IF NOT EXISTS idx_msg_user ON server_messages(user_id, created);
    CREATE INDEX IF NOT EXISTS idx_msg_bad ON server_messages(guild_id, has_bad_words);
    """)
    c.execute("DROP TABLE IF EXISTS user_geo")
    c.execute("DROP TABLE IF EXISTS geo_tokens")
    c.commit()


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30.0)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA busy_timeout=30000;")
        _conn.executescript(SCHEMA)
        _conn.commit()
        _migrate(_conn)
    return _conn


def now() -> float:
    return time.time()


def kv_get(key: str, default=None):
    row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value) -> None:
    conn().execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn().commit()


_DEFAULT_MOOD = {"label": "neutral", "intensity": 0.4, "valence": 0.0}


def mood_get(guild_id: str) -> dict:
    raw = kv_get(f"mood:{guild_id}")
    if not raw:
        return {**_DEFAULT_MOOD, "updated": now()}
    try:
        d = json.loads(raw)
        return {**_DEFAULT_MOOD, **d}
    except (ValueError, TypeError):
        return {**_DEFAULT_MOOD, "updated": now()}


def mood_set(guild_id: str, label: str, intensity: float, valence: float) -> None:
    kv_set(f"mood:{guild_id}", json.dumps({
        "label": str(label)[:24], "intensity": float(intensity),
        "valence": float(valence), "updated": now(),
    }))


def mood_nudge(guild_id: str, dv: float) -> None:
    d = mood_get(guild_id)
    v = max(-1.0, min(1.0, float(d.get("valence", 0.0)) + dv))
    mood_set(guild_id, d.get("label", "neutral"), d.get("intensity", 0.4), v)


_DEFAULT_GUILD = {
    "persona": "",
    "lurk": False,
    "lurk_channel": "",
    "swear_level": "full",
    "allowed_channels": [],
    "smart_always": True,
}


def guild_settings(guild_id: str) -> dict:
    hit = _gs_cache.get(guild_id)
    if hit is not None and time.time() - hit[0] < _GS_TTL:
        return dict(hit[1])
    row = conn().execute(
        "SELECT data FROM guild_settings WHERE guild_id=?", (guild_id,)
    ).fetchone()
    if not row:
        d = dict(_DEFAULT_GUILD)
    else:
        try:
            d = {**_DEFAULT_GUILD, **json.loads(row["data"])}
        except (ValueError, TypeError):
            d = dict(_DEFAULT_GUILD)
    _gs_cache[guild_id] = (time.time(), d)
    return dict(d)


def guild_settings_set(guild_id: str, **patch) -> dict:
    cur = guild_settings(guild_id)
    cur.update(patch)
    conn().execute(
        "INSERT INTO guild_settings(guild_id,data) VALUES(?,?) "
        "ON CONFLICT(guild_id) DO UPDATE SET data=excluded.data",
        (guild_id, json.dumps(cur)),
    )
    conn().commit()
    _gs_cache[guild_id] = (time.time(), dict(cur))
    return cur


def _bond_label(score: float) -> str:
    if score >= 0.7:
        return "ride-or-die"
    if score >= 0.35:
        return "friend"
    if score >= 0.1:
        return "cool with"
    if score > -0.1:
        return "stranger"
    if score > -0.35:
        return "annoying"
    if score > -0.7:
        return "rival"
    return "nemesis"


def relationship_get(user_id: str, guild_id: str) -> dict:
    row = conn().execute(
        "SELECT * FROM relationships WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    ).fetchone()
    if not row:
        return {
            "user_id": user_id, "guild_id": guild_id, "score": 0.0,
            "nickname": None, "grudge": None, "bond_label": "stranger",
            "updated": now(),
        }
    return dict(row)


def relationship_set(
    user_id: str,
    guild_id: str,
    score: Optional[float] = None,
    nickname: Optional[str] = None,
    grudge: Optional[str] = None,
    delta: float = 0.0,
) -> dict:
    cur = relationship_get(user_id, guild_id)
    s = float(cur["score"])
    if score is not None:
        s = float(score)
    s = max(-1.0, min(1.0, s + float(delta)))
    nick = cur.get("nickname")
    if nickname is not None:
        nickname = str(nickname).strip()[:40]
        nick = nickname or None
    g = cur.get("grudge")
    if grudge is not None:
        grudge = str(grudge).strip()[:200]
        g = grudge or None
    label = _bond_label(s)
    conn().execute(
        "INSERT INTO relationships(user_id,guild_id,score,nickname,grudge,bond_label,updated) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id,guild_id) DO UPDATE SET "
        "score=excluded.score, nickname=excluded.nickname, grudge=excluded.grudge, "
        "bond_label=excluded.bond_label, updated=excluded.updated",
        (user_id, guild_id, s, nick, g, label, now()),
    )
    conn().commit()
    return relationship_get(user_id, guild_id)


def relationship_top(guild_id: str, limit: int = 10, worst: bool = False) -> List[dict]:
    order = "ASC" if worst else "DESC"
    rows = conn().execute(
        f"SELECT * FROM relationships WHERE guild_id=? ORDER BY score {order} LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def convo_add(user_id: str, guild_id: str, role: str, content: str) -> None:
    c = conn()
    c.execute(
        "INSERT INTO conversations(user_id,guild_id,role,content,created) VALUES(?,?,?,?,?)",
        (user_id, guild_id, role, (content or "")[:1500], now()),
    )
    keep = max(4, config.CONVO_TURNS * 2)
    c.execute(
        "DELETE FROM conversations WHERE id NOT IN ("
        "  SELECT id FROM conversations WHERE user_id=? AND guild_id=? "
        "  ORDER BY created DESC LIMIT ?"
        ") AND user_id=? AND guild_id=?",
        (user_id, guild_id, keep, user_id, guild_id),
    )
    c.commit()


def convo_get(user_id: str, guild_id: str, limit: int = None) -> List[dict]:
    limit = limit or config.CONVO_TURNS * 2
    rows = conn().execute(
        "SELECT role, content, created FROM conversations "
        "WHERE user_id=? AND guild_id=? ORDER BY created DESC LIMIT ?",
        (user_id, guild_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def convo_clear(user_id: str, guild_id: str) -> int:
    cur = conn().execute(
        "DELETE FROM conversations WHERE user_id=? AND guild_id=?",
        (user_id, guild_id),
    )
    conn().commit()
    return cur.rowcount


def quote_add(guild_id: str, text: str, about: str = None, author: str = None) -> int:
    cur = conn().execute(
        "INSERT INTO quotes(guild_id,text,about,author,created) VALUES(?,?,?,?,?)",
        (guild_id, text.strip()[:500], about, author, now()),
    )
    conn().commit()
    return cur.lastrowid


def quote_random(guild_id: str, about: str = None) -> Optional[dict]:
    if about:
        row = conn().execute(
            "SELECT * FROM quotes WHERE guild_id=? AND about=? ORDER BY RANDOM() LIMIT 1",
            (guild_id, about),
        ).fetchone()
    else:
        row = conn().execute(
            "SELECT * FROM quotes WHERE guild_id=? ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        ).fetchone()
    return dict(row) if row else None


def quote_list(guild_id: str, limit: int = 20) -> List[dict]:
    rows = conn().execute(
        "SELECT * FROM quotes WHERE guild_id=? ORDER BY created DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def quote_delete(qid: int) -> bool:
    cur = conn().execute("DELETE FROM quotes WHERE id=?", (qid,))
    conn().commit()
    return cur.rowcount > 0


_SNOWFLAKE = re.compile(r"(\d{15,22})")


def normalize_subject(about, default_user: str = None) -> str:
    """Canonical subject key: raw user id, or 'server'.

    The model sometimes emits <@id>, bare ids, or 'me'/'user' — normalize so
    erase/list/get all hit the same rows.
    """
    s = str(about if about is not None else "server").strip()
    if not s:
        return "server"
    low = s.lower()
    if low in ("server", "guild", "channel", "here", "this server"):
        return "server"
    if low in ("me", "user", "them", "this user", "the user", "speaker", "author", "self"):
        return str(default_user) if default_user else "server"
    m = _SNOWFLAKE.search(s)
    if m:
        return m.group(1)
    return s


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def add_memory(content, author, guild_id, subject="server", importance=0.5) -> int:
    """Insert a memory, merging into a near-duplicate if one exists."""
    content = (content or "").strip()
    if not content:
        return 0
    subject = normalize_subject(subject, default_user=author)
    guild_id = str(guild_id) if guild_id is not None else None
    importance = max(0.0, min(1.0, float(importance)))
    existing = memories_about(subject, guild_id)
    new_tok = _tokens(content)
    if new_tok:
        for row in existing:
            old_tok = _tokens(row["content"])
            if not old_tok:
                continue
            overlap = len(new_tok & old_tok) / max(1, len(new_tok | old_tok))
            if overlap >= 0.45:
                new_imp = max(float(row["importance"] or 0.5), importance)
                new_imp = min(1.0, new_imp + 0.05)
                text = content if len(content) >= len(row["content"]) else row["content"]
                conn().execute(
                    "UPDATE memories SET content=?, importance=?, updated=?, author=? WHERE id=?",
                    (text, new_imp, now(), author, row["id"]),
                )
                conn().commit()
                return row["id"]
    cur = conn().execute(
        "INSERT INTO memories(subject,content,author,guild_id,importance,created,updated) "
        "VALUES(?,?,?,?,?,?,?)",
        (subject, content, author, guild_id, importance, now(), now()),
    )
    conn().commit()
    _enforce_memory_cap(subject, guild_id)
    return cur.lastrowid


def _enforce_memory_cap(subject: str, guild_id: str) -> None:
    rows = memories_about(subject, guild_id)
    cap = config.MEMORY_SOFT_CAP
    if len(rows) <= cap:
        return
    extras = rows[cap:]
    for r in extras:
        if float(r["importance"] or 0) < 0.35:
            conn().execute("DELETE FROM memories WHERE id=?", (r["id"],))
        else:
            new_imp = max(0.05, float(r["importance"] or 0.5) * 0.85)
            conn().execute(
                "UPDATE memories SET importance=? WHERE id=?",
                (new_imp, r["id"]),
            )
    conn().commit()


def update_memory(mem_id: int, content: str = None, importance: float = None) -> bool:
    row = conn().execute("SELECT * FROM memories WHERE id=?", (mem_id,)).fetchone()
    if not row:
        return False
    content = content if content is not None else row["content"]
    importance = float(importance) if importance is not None else float(row["importance"])
    conn().execute(
        "UPDATE memories SET content=?, importance=?, updated=? WHERE id=?",
        (content.strip(), max(0.0, min(1.0, importance)), now(), mem_id),
    )
    conn().commit()
    return True


def memories_about(subject: str, guild_id: Optional[str]) -> List[sqlite3.Row]:
    subject = normalize_subject(subject)
    gid = str(guild_id) if guild_id is not None else None
    key = ("about", subject, gid)
    cached = _mem_cache_get(key)
    if cached is not None:
        return cached
    rows = conn().execute(
        "SELECT * FROM memories WHERE subject=? AND "
        "(guild_id=? OR guild_id IS NULL OR guild_id='' OR guild_id='dm') "
        "ORDER BY importance DESC, created DESC",
        (subject, gid),
    ).fetchall()
    _mem_cache_set(key, rows)
    return rows


def scope_memories(guild_id: Optional[str]) -> List[sqlite3.Row]:
    if guild_id is None:
        return conn().execute("SELECT * FROM memories").fetchall()
    gid = str(guild_id)
    key = ("scope", gid)
    cached = _mem_cache_get(key)
    if cached is not None:
        return cached
    rows = conn().execute(
        "SELECT * FROM memories WHERE guild_id=? OR guild_id IS NULL OR guild_id='' OR guild_id='dm'",
        (gid,),
    ).fetchall()
    _mem_cache_set(key, rows)
    return rows


def get_memory(mem_id: int) -> Optional[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM memories WHERE id=?", (int(mem_id),)
    ).fetchone()


def forget_memory(mem_id: int) -> bool:
    cur = conn().execute("DELETE FROM memories WHERE id=?", (int(mem_id),))
    conn().commit()
    return cur.rowcount > 0


def forget_memories_about(
    subject: str,
    guild_id: Optional[str],
    *,
    clear_convo: bool = True,
    all_guilds: bool = False,
) -> dict:
    """Wipe long-term memories about a subject.

    Also clears short-term conversation history for that user (so the model
    cannot re-learn the same facts on the next message). Returns counts.
    """
    subject = normalize_subject(subject)
    gid = str(guild_id) if guild_id is not None else None
    if all_guilds or gid is None:
        cur = conn().execute("DELETE FROM memories WHERE subject=?", (subject,))
    else:
        cur = conn().execute(
            "DELETE FROM memories WHERE subject=? AND "
            "(guild_id=? OR guild_id IS NULL OR guild_id='' OR guild_id='dm')",
            (subject, gid),
        )
    n_mem = cur.rowcount
    n_convo = 0
    if clear_convo and subject.isdigit():
        if all_guilds or gid is None:
            cur2 = conn().execute(
                "DELETE FROM conversations WHERE user_id=?", (subject,)
            )
            n_convo = cur2.rowcount
        else:
            n_convo = convo_clear(subject, gid)
    conn().commit()
    return {"memories": n_mem, "convo": n_convo}


def compact_memories(subject: str, guild_id: str, keep: int = 15) -> int:
    """Keep top-N by importance; delete the rest. Returns deleted count."""
    subject = normalize_subject(subject)
    rows = memories_about(subject, guild_id)
    if len(rows) <= keep:
        return 0
    drop_ids = [r["id"] for r in rows[keep:]]
    for i in drop_ids:
        conn().execute("DELETE FROM memories WHERE id=?", (i,))
    conn().commit()
    return len(drop_ids)


def add_lesson(content: str, source: str = "reflection") -> bool:
    global _lessons_cache
    try:
        conn().execute(
            "INSERT INTO lessons(content,source,created) VALUES(?,?,?)",
            (content.strip(), source, now()),
        )
        conn().commit()
        _lessons_cache = None
        return True
    except sqlite3.IntegrityError:
        return False


def all_lessons():
    global _lessons_cache, _lessons_ts
    now_t = time.time()
    if _lessons_cache is not None and now_t - _lessons_ts < _LESSONS_TTL:
        return _lessons_cache
    _lessons_cache = conn().execute("SELECT * FROM lessons ORDER BY created").fetchall()
    _lessons_ts = now_t
    return _lessons_cache


def delete_lesson(lesson_id: int) -> bool:
    global _lessons_cache
    cur = conn().execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    conn().commit()
    if cur.rowcount:
        _lessons_cache = None
    return cur.rowcount > 0


def add_feedback(user_msg, bot_msg, verdict, author, note=None) -> int:
    cur = conn().execute(
        "INSERT INTO feedback(user_msg,bot_msg,verdict,note,author,created) "
        "VALUES(?,?,?,?,?,?)",
        (user_msg, bot_msg, verdict, note, author, now()),
    )
    conn().commit()
    return cur.lastrowid


def unprocessed_feedback(limit: int):
    return conn().execute(
        "SELECT * FROM feedback WHERE processed=0 ORDER BY created LIMIT ?", (limit,)
    ).fetchall()


def mark_feedback_processed(ids) -> None:
    if not ids:
        return
    conn().executemany("UPDATE feedback SET processed=1 WHERE id=?", [(i,) for i in ids])
    conn().commit()


def add_command(name, description, behavior, author, guild_id) -> None:
    conn().execute(
        "INSERT INTO commands(name,description,behavior,author,guild_id,created) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "description=excluded.description, behavior=excluded.behavior",
        (name.lower(), description, behavior, author, guild_id, now()),
    )
    conn().commit()


def get_command(name):
    return conn().execute("SELECT * FROM commands WHERE name=?", (name.lower(),)).fetchone()


def all_commands():
    return conn().execute("SELECT * FROM commands ORDER BY uses DESC").fetchall()


def bump_command(name) -> None:
    conn().execute("UPDATE commands SET uses=uses+1 WHERE name=?", (name.lower(),))
    conn().commit()


def delete_command(name) -> bool:
    cur = conn().execute("DELETE FROM commands WHERE name=?", (name.lower(),))
    conn().commit()
    return cur.rowcount > 0


def log_interaction(kind, author, guild_id) -> None:
    conn().execute(
        "INSERT INTO interactions(kind,author,guild_id,created) VALUES(?,?,?,?)",
        (kind, author, guild_id, now()),
    )
    conn().commit()


def stats() -> dict:
    c = conn()
    q = lambda sql, *a: c.execute(sql, a).fetchone()["n"]
    return {
        "interactions": q("SELECT COUNT(*) n FROM interactions"),
        "memories": q("SELECT COUNT(*) n FROM memories"),
        "lessons": q("SELECT COUNT(*) n FROM lessons"),
        "commands": q("SELECT COUNT(*) n FROM commands"),
        "quotes": q("SELECT COUNT(*) n FROM quotes"),
        "relationships": q("SELECT COUNT(*) n FROM relationships"),
        "thumbs_up": q("SELECT COUNT(*) n FROM feedback WHERE verdict='up'"),
        "thumbs_down": q("SELECT COUNT(*) n FROM feedback WHERE verdict='down'"),
    }


def export_guild(guild_id: str) -> dict:
    """Dump brain data for one guild (and global lessons)."""
    return {
        "version": 2,
        "guild_id": guild_id,
        "exported_at": now(),
        "settings": guild_settings(guild_id),
        "memories": [dict(r) for r in scope_memories(guild_id) if r["guild_id"] == guild_id],
        "lessons": [dict(r) for r in all_lessons()],
        "commands": [dict(r) for r in all_commands() if (r["guild_id"] or "") == guild_id],
        "quotes": quote_list(guild_id, limit=500),
        "relationships": [
            dict(r) for r in conn().execute(
                "SELECT * FROM relationships WHERE guild_id=?", (guild_id,)
            ).fetchall()
        ],
    }


def import_guild(data: dict, guild_id: str) -> dict:
    """Import export payload into this guild. Returns counts."""
    counts = {"memories": 0, "lessons": 0, "commands": 0, "quotes": 0, "relationships": 0}
    if not isinstance(data, dict):
        return counts
    if isinstance(data.get("settings"), dict):
        guild_settings_set(guild_id, **{k: v for k, v in data["settings"].items()
                                        if k in _DEFAULT_GUILD})
    for m in data.get("memories") or []:
        if not isinstance(m, dict) or not m.get("content"):
            continue
        add_memory(
            m["content"], m.get("author") or "import", guild_id,
            subject=m.get("subject") or "server",
            importance=float(m.get("importance", 0.5)),
        )
        counts["memories"] += 1
    for l in data.get("lessons") or []:
        content = l.get("content") if isinstance(l, dict) else l
        if content and add_lesson(str(content), source="import"):
            counts["lessons"] += 1
    for c in data.get("commands") or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        add_command(
            c["name"], c.get("description") or c["name"],
            c.get("behavior") or "Respond helpfully.",
            c.get("author") or "import", guild_id,
        )
        counts["commands"] += 1
    for q in data.get("quotes") or []:
        if not isinstance(q, dict) or not q.get("text"):
            continue
        quote_add(guild_id, q["text"], about=q.get("about"), author=q.get("author"))
        counts["quotes"] += 1
    for r in data.get("relationships") or []:
        if not isinstance(r, dict) or not r.get("user_id"):
            continue
        relationship_set(
            str(r["user_id"]), guild_id,
            score=float(r.get("score", 0)),
            nickname=r.get("nickname"),
            grudge=r.get("grudge"),
        )
        counts["relationships"] += 1
    return counts


def user_flag_get(user_id: str, key: str, default: str = None) -> Optional[str]:
    return kv_get(f"uf:{user_id}:{key}", default)


def user_flag_set(user_id: str, key: str, value) -> None:
    kv_set(f"uf:{user_id}:{key}", value)


def user_flag_int(user_id: str, key: str, default: int = 0) -> int:
    raw = user_flag_get(user_id, key)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


_BAD_PATTERNS = [
    r"\bfuck\w*", r"\bshit\w*", r"\bbitch\w*", r"\basshole\w*", r"\bbastard\w*",
    r"\bdick\w*", r"\bpussy\w*", r"\bcunt\w*", r"\bwhore\w*", r"\bslut\w*",
    r"\bnigger\w*", r"\bnigga\w*", r"\bfaggot\w*", r"\bretard\w*", r"\bkys\b",
    r"\bkill\s+your\s*self\b", r"\bstfu\b", r"\bshut\s+the\s+fuck\s+up\b",
    r"\bdumbass\w*", r"\bmotherfucker\w*", r"\bpiece\s+of\s+shit\b"
]
_BAD_RE = re.compile("|".join(_BAD_PATTERNS), re.IGNORECASE)


def detect_bad_words(content: str) -> tuple:
    if not content:
        return False, []
    matches = list(set(_BAD_RE.findall(content.lower())))
    return bool(matches), matches


def record_server_message(
    message_id: str,
    guild_id: str,
    guild_name: str,
    channel_id: str,
    channel_name: str,
    user_id: str,
    username: str,
    display_name: str,
    content: str,
) -> None:
    if not content or not user_id:
        return
    has_bad, matches = detect_bad_words(content)
    bad_str = json.dumps(matches) if has_bad else ""
    c = conn()
    c.execute(
        """
        INSERT INTO server_messages (
            message_id, guild_id, guild_name, channel_id, channel_name,
            user_id, username, display_name, content, has_bad_words,
            bad_words_found, created
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            content=excluded.content,
            display_name=excluded.display_name,
            has_bad_words=excluded.has_bad_words,
            bad_words_found=excluded.bad_words_found
        """,
        (
            str(message_id), str(guild_id), str(guild_name or "DM/Unknown"),
            str(channel_id), str(channel_name or "unknown"), str(user_id),
            str(username), str(display_name or username), str(content)[:2000],
            1 if has_bad else 0, bad_str, now()
        )
    )
    c.commit()


_STOP_WORDS = {
    "and", "the", "to", "a", "of", "in", "is", "you", "i", "it", "that", "for",
    "on", "my", "me", "we", "they", "are", "was", "this", "with", "your", "at",
    "be", "but", "so", "like", "just", "have", "has", "not", "do", "did", "im",
    "u", "ur", "what", "when", "who", "how", "why", "can", "cant", "dont", "get",
    "go", "up", "out", "off", "no", "yeah", "yes", "ok", "okay", "lol", "lmao",
    "fr", "ngl", "tbh", "rn", "bro", "man", "guys", "about", "there", "here",
    "them", "him", "her", "his", "their", "would", "could", "should", "will",
    "am", "an", "or", "as", "if", "than", "then", "too", "very", "really",
    "actually", "know", "think", "say", "said", "says", "want", "need", "let",
    "tell", "make", "made", "come", "came", "see", "look", "back", "been",
    "being", "from", "into", "over", "under", "again", "more", "most", "some",
    "any", "all", "every", "one", "two", "time", "day", "still", "even",
    "though", "though", "thing", "things", "cause", "cuz", "cause", "gonna",
    "wanna", "gotta", "dont", "didnt", "doesnt", "wasnt", "isnt", "arent",
    "aint", "idk", "ikr", "omg", "wtf", "bruh", "dude", "yall", "y'all",
}


def _top_words(rows: List[sqlite3.Row], n: int = 20) -> List[str]:
    """Most common non-stop words across a batch of recorded messages."""
    from collections import Counter
    cnt = Counter()
    for r in rows:
        content = r["content"] or ""
        cnt.update(
            w for w in _WORD.findall(content.lower())
            if w not in _STOP_WORDS
        )
    return [w for w, _ in cnt.most_common(n)]


def get_user_intelligence(user_id: str, guild_id: Optional[str] = None) -> dict:
    """Full recorded history for a user — totals, monthly activity, channels,
    favorite words, flagged messages, recent + random old message samples."""
    c = conn()
    uid = str(user_id)
    gid = str(guild_id) if guild_id and guild_id != "dm" else None
    w = "user_id=?"
    args = [uid]
    if gid:
        w += " AND guild_id=?"
        args.append(gid)

    def q(sql: str, extra: str = "", limit: int = None):
        lim = f" LIMIT {int(limit)}" if limit else ""
        return c.execute(sql.format(w=w) + extra + lim, tuple(args))

    total_row = q("SELECT COUNT(*) n FROM server_messages WHERE {w}").fetchone()
    bad_row = q("SELECT COUNT(*) n FROM server_messages WHERE {w} AND has_bad_words=1").fetchone()
    ts = q("SELECT MIN(created) min_ts, MAX(created) max_ts FROM server_messages WHERE {w}").fetchone()
    day_row = q(
        "SELECT COUNT(DISTINCT strftime('%Y-%m-%d', created, 'unixepoch')) d "
        "FROM server_messages WHERE {w}"
    ).fetchone()
    len_row = q(
        "SELECT AVG(LENGTH(content)) avg_len, MAX(LENGTH(content)) max_len "
        "FROM server_messages WHERE {w}"
    ).fetchone()
    bad_msgs = q(
        "SELECT channel_name, content, bad_words_found, created FROM server_messages "
        "WHERE {w} AND has_bad_words=1 ORDER BY created DESC", limit=15
    ).fetchall()
    recent_msgs = q(
        "SELECT channel_name, content, created FROM server_messages "
        "WHERE {w} ORDER BY created DESC", limit=60
    ).fetchall()
    sample_msgs = q(
        "SELECT channel_name, content, created FROM ("
        "SELECT channel_name, content, created FROM server_messages "
        "WHERE {w} ORDER BY created DESC LIMIT 5000"
        ") ORDER BY RANDOM()", limit=12
    ).fetchall()
    monthly = q(
        "SELECT strftime('%Y-%m', created, 'unixepoch') m, COUNT(*) n "
        "FROM server_messages WHERE {w} GROUP BY m ORDER BY m DESC", limit=12
    ).fetchall()
    channels = q(
        "SELECT channel_name, COUNT(*) n FROM server_messages WHERE {w} "
        "GROUP BY channel_name ORDER BY n DESC", limit=8
    ).fetchall()
    word_rows = q(
        "SELECT content FROM server_messages WHERE {w} ORDER BY created DESC", limit=2000
    ).fetchall()
    user_info = c.execute(
        "SELECT username, display_name FROM server_messages WHERE user_id=? ORDER BY created DESC LIMIT 1",
        (uid,),
    ).fetchone()

    return {
        "user_id": uid,
        "username": user_info["username"] if user_info else uid,
        "display_name": user_info["display_name"] if user_info else uid,
        "total_messages": total_row["n"] if total_row else 0,
        "bad_message_count": bad_row["n"] if bad_row else 0,
        "first_seen": ts["min_ts"] if ts else None,
        "last_seen": ts["max_ts"] if ts else None,
        "active_days": day_row["d"] if day_row else 0,
        "avg_len": int(len_row["avg_len"] or 0) if len_row else 0,
        "max_len": int(len_row["max_len"] or 0) if len_row else 0,
        "bad_messages": [dict(r) for r in bad_msgs],
        "recent_messages": [dict(r) for r in recent_msgs],
        "sample_messages": [dict(r) for r in sample_msgs],
        "monthly": [{"month": r["m"], "n": r["n"]} for r in monthly],
        "channels": [{"channel_name": r["channel_name"] or "unknown", "n": r["n"]} for r in channels],
        "top_words": _top_words(word_rows, 20),
    }


def get_user_bad_messages(user_id: str, guild_id: Optional[str] = None, limit: int = 20) -> List[dict]:
    c = conn()
    uid = str(user_id)
    gid = str(guild_id) if guild_id and guild_id != "dm" else None
    if gid:
        rows = c.execute(
            "SELECT channel_name, content, bad_words_found, created FROM server_messages "
            "WHERE user_id=? AND guild_id=? AND has_bad_words=1 ORDER BY created DESC LIMIT ?",
            (uid, gid, limit)
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT channel_name, content, bad_words_found, created FROM server_messages "
            "WHERE user_id=? AND has_bad_words=1 ORDER BY created DESC LIMIT ?",
            (uid, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def get_server_intelligence(guild_id: str) -> dict:
    """Full recorded history for a server — totals, active users, monthly
    activity, top channels, top words, top senders, flagged messages."""
    c = conn()
    gid = str(guild_id)
    total = c.execute("SELECT COUNT(*) n FROM server_messages WHERE guild_id=?", (gid,)).fetchone()["n"]
    bad_total = c.execute("SELECT COUNT(*) n FROM server_messages WHERE guild_id=? AND has_bad_words=1", (gid,)).fetchone()["n"]
    users = c.execute("SELECT COUNT(DISTINCT user_id) n FROM server_messages WHERE guild_id=?", (gid,)).fetchone()["n"]
    ts = c.execute(
        "SELECT MIN(created) min_ts, MAX(created) max_ts FROM server_messages WHERE guild_id=?", (gid,)
    ).fetchone()
    top_senders = c.execute(
        "SELECT user_id, username, display_name, COUNT(*) cnt, "
        "SUM(has_bad_words) bad_cnt FROM server_messages WHERE guild_id=? "
        "GROUP BY user_id ORDER BY cnt DESC LIMIT 10", (gid,)
    ).fetchall()
    recent_bad = c.execute(
        "SELECT username, display_name, channel_name, content, bad_words_found, created "
        "FROM server_messages WHERE guild_id=? AND has_bad_words=1 ORDER BY created DESC LIMIT 10", (gid,)
    ).fetchall()
    monthly = c.execute(
        "SELECT strftime('%Y-%m', created, 'unixepoch') m, COUNT(*) n "
        "FROM server_messages WHERE guild_id=? GROUP BY m ORDER BY m DESC LIMIT 12", (gid,)
    ).fetchall()
    channels = c.execute(
        "SELECT channel_name, COUNT(*) n FROM server_messages WHERE guild_id=? "
        "GROUP BY channel_name ORDER BY n DESC LIMIT 8", (gid,)
    ).fetchall()
    word_rows = c.execute(
        "SELECT content FROM server_messages WHERE guild_id=? ORDER BY created DESC LIMIT 3000", (gid,)
    ).fetchall()
    return {
        "guild_id": gid,
        "total_messages": total,
        "bad_messages_total": bad_total,
        "active_users": users,
        "first_seen": ts["min_ts"] if ts else None,
        "last_seen": ts["max_ts"] if ts else None,
        "top_senders": [dict(r) for r in top_senders],
        "recent_bad_messages": [dict(r) for r in recent_bad],
        "monthly": [{"month": r["m"], "n": r["n"]} for r in monthly],
        "channels": [{"channel_name": r["channel_name"] or "unknown", "n": r["n"]} for r in channels],
        "top_words": _top_words(word_rows, 20),
    }


def find_user_by_name(query: str, guild_id: Optional[str] = None) -> Optional[dict]:
    """Look up recorded user by username, display_name, or user ID."""
    if not query:
        return None
    c = conn()
    q = query.strip().lstrip("@")
    m = _SNOWFLAKE.search(q)
    if m:
        uid = m.group(1)
        row = c.execute("SELECT user_id, username, display_name FROM server_messages WHERE user_id=? LIMIT 1", (uid,)).fetchone()
        if row:
            return dict(row)
        return {"user_id": uid, "username": uid, "display_name": uid}

    gid = str(guild_id) if guild_id and guild_id != "dm" else None
    if gid:
        row = c.execute(
            "SELECT user_id, username, display_name FROM server_messages "
            "WHERE guild_id=? AND (username LIKE ? OR display_name LIKE ?) ORDER BY created DESC LIMIT 1",
            (gid, f"%{q}%", f"%{q}%")
        ).fetchone()
        if row:
            return dict(row)

    row = c.execute(
        "SELECT user_id, username, display_name FROM server_messages "
        "WHERE username LIKE ? OR display_name LIKE ? ORDER BY created DESC LIMIT 1",
        (f"%{q}%", f"%{q}%")
    ).fetchone()
    return dict(row) if row else None
