import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DATABASE_PATH, DEFAULT_INTERVAL_MINUTES, SOURCE_CHANNEL_ID


def is_source(chat_id) -> bool:
    """The source channel is where posts are collected from; it must never be a destination."""
    return bool(SOURCE_CHANNEL_ID) and int(chat_id) == SOURCE_CHANNEL_ID


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect():
    parent = os.path.dirname(DATABASE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


@contextmanager
def connection():
    db = _connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL DEFAULT 200,
            next_run TEXT,
            kind TEXT NOT NULL DEFAULT 'channel',
            thread_id INTEGER,
            paused INTEGER NOT NULL DEFAULT 0,
            batch_size INTEGER
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            caption TEXT,
            entities_json TEXT,
            reply_markup_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS channel_queue (
            channel_id INTEGER NOT NULL,
            queue_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            PRIMARY KEY(channel_id, queue_id),
            UNIQUE(channel_id, position)
        );
        CREATE TABLE IF NOT EXISTS channel_sent (
            channel_id INTEGER NOT NULL,
            queue_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(channel_id, queue_id)
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Upgrade databases created by older versions.
        cols = {r["name"] for r in db.execute("PRAGMA table_info(channels)")}
        for name, ddl in (
            ("kind", "TEXT NOT NULL DEFAULT 'channel'"),
            ("thread_id", "INTEGER"),
            ("paused", "INTEGER NOT NULL DEFAULT 0"),
            ("batch_size", "INTEGER"),
        ):
            if name not in cols:
                db.execute(f"ALTER TABLE channels ADD COLUMN {name} {ddl}")


# ------------------------------------------------- destinations (channels + groups)

def register_channel_if_missing(chat_id: int, title: str, kind: str = "channel", thread_id=None):
    if is_source(chat_id):
        return
    with connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO channels(chat_id,title,interval_minutes,next_run,kind,thread_id) VALUES(?,?,?,?,?,?)",
            (chat_id, title or str(chat_id), DEFAULT_INTERVAL_MINUTES, _now(), kind, thread_id),
        )


def add_channel(chat_id: int, title: str, interval_minutes: int, kind: str = "channel", thread_id=None):
    if is_source(chat_id):
        return
    with connection() as db:
        db.execute(
            """INSERT INTO channels(chat_id,title,interval_minutes,next_run,kind,thread_id) VALUES(?,?,?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                   interval_minutes=excluded.interval_minutes, kind=excluded.kind, thread_id=excluded.thread_id""",
            (chat_id, title, interval_minutes, _now(), kind, thread_id),
        )


def _update_all_or_one(db, column: str, value, target) -> int:
    if target == "all":
        return db.execute(f"UPDATE channels SET {column}=?", (value,)).rowcount
    return db.execute(f"UPDATE channels SET {column}=? WHERE chat_id=?", (value, int(target))).rowcount


def set_interval(target, minutes: int) -> int:
    with connection() as db:
        return _update_all_or_one(db, "interval_minutes", minutes, target)


def set_batch_size(target, size: int) -> int:
    with connection() as db:
        return _update_all_or_one(db, "batch_size", size, target)


def set_paused(target, paused: bool) -> int:
    with connection() as db:
        return _update_all_or_one(db, "paused", 1 if paused else 0, target)


def send_now(target) -> int:
    with connection() as db:
        return _update_all_or_one(db, "next_run", _now(), target)


def list_channels():
    with connection() as db:
        return db.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM channel_queue cq WHERE cq.channel_id=c.chat_id) AS pending
               FROM channels c WHERE c.chat_id<>? ORDER BY c.kind, c.title""",
            (SOURCE_CHANNEL_ID,),
        ).fetchall()


def library_size() -> int:
    with connection() as db:
        return db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]


def remove_channel(chat_id: int):
    with connection() as db:
        db.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM channel_queue WHERE channel_id=?", (chat_id,))
        db.execute("DELETE FROM channel_sent WHERE channel_id=?", (chat_id,))


def migrate_channel(old_id: int, new_id: int):
    """A group was upgraded to a supergroup, so Telegram gave it a new chat ID."""
    with connection() as db:
        exists = db.execute("SELECT 1 FROM channels WHERE chat_id=?", (new_id,)).fetchone()
        if exists:
            db.execute("DELETE FROM channels WHERE chat_id=?", (old_id,))
            db.execute("DELETE FROM channel_queue WHERE channel_id=?", (old_id,))
            db.execute("DELETE FROM channel_sent WHERE channel_id=?", (old_id,))
            return
        db.execute("UPDATE channels SET chat_id=?, kind='supergroup' WHERE chat_id=?", (new_id, old_id))
        db.execute("UPDATE channel_queue SET channel_id=? WHERE channel_id=?", (new_id, old_id))
        db.execute("UPDATE channel_sent SET channel_id=? WHERE channel_id=?", (new_id, old_id))


# ------------------------------------------------------------------- posts

def _next_round_robin_channel(db):
    ids = [
        int(r["chat_id"])
        for r in db.execute("SELECT chat_id FROM channels WHERE paused=0 AND chat_id<>? ORDER BY chat_id", (SOURCE_CHANNEL_ID,))
    ]
    if not ids:
        return None
    row = db.execute("SELECT value FROM settings WHERE key='rr_last'").fetchone()
    last = int(row["value"]) if row else None
    chosen = next((c for c in ids if last is None or c > last), ids[0])
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('rr_last',?)", (str(chosen),))
    return chosen


def _append_to_channel_queue(db, channel_id: int, queue_id: int):
    pos = db.execute(
        "SELECT COALESCE(MAX(position),0)+1 AS p FROM channel_queue WHERE channel_id=?", (channel_id,)
    ).fetchone()["p"]
    db.execute(
        "INSERT OR IGNORE INTO channel_queue(channel_id,queue_id,position) VALUES(?,?,?)",
        (channel_id, queue_id, pos),
    )


def add_post(source_chat_id: int, message_id: int, caption, entities_json, reply_markup_json):
    """Save a post in the library and give it to ONE active destination (round-robin).
    Returns the new queue id, or None if this exact post was already saved."""
    with connection() as db:
        cur = db.execute(
            """INSERT OR IGNORE INTO queue(source_chat_id,message_id,caption,entities_json,reply_markup_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (source_chat_id, message_id, caption, entities_json, reply_markup_json, _now()),
        )
        if cur.rowcount == 0:
            return None
        queue_id = cur.lastrowid
        channel_id = _next_round_robin_channel(db)
        if channel_id is not None:
            _append_to_channel_queue(db, channel_id, queue_id)
        return queue_id


def delete_post(queue_id: int):
    """Remove a post everywhere (used when the source message no longer exists)."""
    with connection() as db:
        db.execute("DELETE FROM queue WHERE id=?", (queue_id,))
        db.execute("DELETE FROM channel_queue WHERE queue_id=?", (queue_id,))
        db.execute("DELETE FROM channel_sent WHERE queue_id=?", (queue_id,))


def _pick_random(db, channel_id: int, exclude_id=None):
    sql = """SELECT q.* FROM queue q
             WHERE q.id NOT IN (SELECT queue_id FROM channel_sent WHERE channel_id=?)
               AND q.id NOT IN (SELECT queue_id FROM channel_queue WHERE channel_id=?)"""
    params = [channel_id, channel_id]
    if exclude_id is not None:
        sql += " AND q.id<>?"
        params.append(exclude_id)
    sql += " ORDER BY RANDOM() LIMIT 1"
    return db.execute(sql, params).fetchone()


def get_next_post(channel_id: int):
    """Next post for a destination.
    1) its own pending queue, in order;
    2) when empty: a random post it has NOT sent yet in the current cycle;
    3) when every post has been sent: start a new cycle (never the same post twice in a row)."""
    with connection() as db:
        row = db.execute(
            """SELECT q.* FROM queue q JOIN channel_queue cq ON cq.queue_id=q.id
               WHERE cq.channel_id=? ORDER BY cq.position LIMIT 1""",
            (channel_id,),
        ).fetchone()
        if row:
            return row

        row = _pick_random(db, channel_id)
        if row is None:
            total = db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
            if total == 0:
                return None
            last = db.execute(
                "SELECT queue_id FROM channel_sent WHERE channel_id=? ORDER BY sent_at DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            db.execute("DELETE FROM channel_sent WHERE channel_id=?", (channel_id,))
            exclude = int(last["queue_id"]) if (last and total > 1) else None
            row = _pick_random(db, channel_id, exclude)
        if row is None:
            return None
        _append_to_channel_queue(db, channel_id, int(row["id"]))
        return row


def finish_channel_post(channel_id: int, queue_id: int):
    with connection() as db:
        db.execute("DELETE FROM channel_queue WHERE channel_id=? AND queue_id=?", (channel_id, queue_id))
        db.execute(
            "INSERT OR REPLACE INTO channel_sent(channel_id,queue_id,sent_at) VALUES(?,?,?)",
            (channel_id, queue_id, _now()),
        )


# --------------------------------------------------------------- scheduling

def next_due_channel(now_iso: str):
    with connection() as db:
        return db.execute(
            "SELECT * FROM channels WHERE paused=0 AND chat_id<>? AND (next_run IS NULL OR next_run<=?) "
            "ORDER BY COALESCE(next_run,'') LIMIT 1",
            (SOURCE_CHANNEL_ID, now_iso),
        ).fetchone()


def postpone_channel(channel_id: int, next_run: str):
    with connection() as db:
        db.execute("UPDATE channels SET next_run=? WHERE chat_id=?", (next_run, channel_id))
