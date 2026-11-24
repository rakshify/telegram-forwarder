"""Per-source-account SQLite state (e.g. wa_state_<short_id>.sqlite).

inbox   Every message from a configured source chat is written here the
        moment it arrives, before any download or upload, as the source's
        own payload (the source decodes it later). Some platforms count a
        message as delivered once the client receives it and won't send it
        again (WhatsApp does), so this queue is what makes a crash or
        restart mid-forward lose nothing. One worker drains it in arrival
        order.

msgmap  Source message id -> the destination's message id (a Telegram
        message id, or a Signal timestamp), per pair. Used to turn replies
        into replies at the destination, to apply edits, and to never post
        the same message twice (a retry skips pairs already done).

names   Last-seen display name per sender id, for @mentions and for the
        name shown on quoted messages.

cursors The newest message id queued per source chat, for sources that can
        fetch what they missed (Telegram's catch-up after downtime).

The column names (wa_id, tg_id, jid) are kept from the first release, which
was WhatsApp -> Telegram only, so older and newer versions read the same file.

All access happens on the event-loop thread; each statement is its own
transaction (autocommit) and the file is in WAL mode.
"""
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union

# One message id can arrive as several events with different payloads
# (WhatsApp sends an encryption-key handover, then the content), so queue
# entries are unique per (source, id, payload digest), not per id alone.
# Posting stays exactly-once through msgmap, which is keyed by message id.
_INBOX_TABLE = """
CREATE TABLE IF NOT EXISTS inbox (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT    NOT NULL,
    msg_id      TEXT    NOT NULL,
    digest      TEXT    NOT NULL DEFAULT '',
    raw         BLOB    NOT NULL,
    received_at REAL    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending | done | failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    UNIQUE (source, msg_id, digest)
)"""
_INBOX_INDEX = "CREATE INDEX IF NOT EXISTS inbox_by_status ON inbox (status, seq)"

_SCHEMA = _INBOX_TABLE + ";\n" + _INBOX_INDEX + """;

CREATE TABLE IF NOT EXISTS msgmap (
    pair_key   TEXT    NOT NULL,
    wa_id      TEXT    NOT NULL,
    tg_id      INTEGER NOT NULL,
    is_media   INTEGER NOT NULL DEFAULT 0,
    created_at REAL    NOT NULL,
    PRIMARY KEY (pair_key, wa_id)
);

CREATE TABLE IF NOT EXISTS names (
    jid        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cursors (
    chat       TEXT PRIMARY KEY,
    last_id    INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""

DONE_RETENTION_S = 7 * 86400      # forwarded/failed queue rows
MAP_RETENTION_S = 180 * 86400     # reply/edit targets


@dataclass
class InboxItem:
    seq: int
    source: str
    msg_id: str
    raw: bytes
    attempts: int


class Store:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(_SCHEMA)
        self._migrate_inbox()

    def _migrate_inbox(self) -> None:
        """Files from the first release keyed the queue by (source, msg_id)."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}
        if "digest" in columns:
            return
        self.db.execute("BEGIN")
        self.db.execute("ALTER TABLE inbox RENAME TO inbox_v1")
        self.db.execute("DROP INDEX IF EXISTS inbox_by_status")
        self.db.execute(_INBOX_TABLE)
        self.db.execute(
            "INSERT INTO inbox (seq, source, msg_id, digest, raw, received_at, status, attempts, last_error) "
            "SELECT seq, source, msg_id, '', raw, received_at, status, attempts, last_error FROM inbox_v1"
        )
        self.db.execute("DROP TABLE inbox_v1")
        self.db.execute(_INBOX_INDEX)
        self.db.execute("COMMIT")

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------- inbox

    def enqueue(self, source: str, msg_id: str, raw: bytes, digest: str = "") -> bool:
        """Queue an event. False if this exact event (same payload) was queued already."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbox (source, msg_id, digest, raw, received_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, msg_id, digest, raw, time.time()),
        )
        return cur.rowcount == 1

    def next_pending(self) -> Optional[InboxItem]:
        row = self.db.execute(
            "SELECT seq, source, msg_id, raw, attempts FROM inbox "
            "WHERE status = 'pending' ORDER BY seq LIMIT 1"
        ).fetchone()
        return InboxItem(*row) if row else None

    def pending_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM inbox WHERE status = 'pending'").fetchone()[0]

    def mark_done(self, seq: int) -> None:
        self.db.execute("UPDATE inbox SET status = 'done', last_error = NULL WHERE seq = ?", (seq,))

    def record_attempt(self, seq: int, error: str) -> int:
        """Count a failed attempt; returns the attempts so far."""
        self.db.execute(
            "UPDATE inbox SET attempts = attempts + 1, last_error = ? WHERE seq = ?",
            (error[:500], seq),
        )
        return self.db.execute("SELECT attempts FROM inbox WHERE seq = ?", (seq,)).fetchone()[0]

    def mark_failed(self, seq: int, error: str) -> None:
        self.db.execute(
            "UPDATE inbox SET status = 'failed', last_error = ? WHERE seq = ?",
            (error[:500], seq),
        )

    # ---------------------------------------------------------- message map

    def posted(self, pair_key: str, msg_id: str) -> Optional[Tuple[int, bool]]:
        """(the destination's message id, is_media) if `msg_id` was posted to that pair."""
        row = self.db.execute(
            "SELECT tg_id, is_media FROM msgmap WHERE pair_key = ? AND wa_id = ?",
            (pair_key, msg_id),
        ).fetchone()
        return (row[0], bool(row[1])) if row else None

    def record_posted(self, pair_key: str, msg_id: str, dest_msg_id: int, is_media: bool) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO msgmap (pair_key, wa_id, tg_id, is_media, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pair_key, msg_id, dest_msg_id, int(is_media), time.time()),
        )

    # ---------------------------------------------------------- names

    def remember_name(self, sender_id: str, name: str) -> None:
        name = (name or "").strip()
        if not sender_id or not name:
            return
        self.db.execute(
            "INSERT INTO names (jid, name, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (jid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at "
            "WHERE names.name != excluded.name",
            (sender_id, name, time.time()),
        )

    def name_for(self, sender_id: str) -> Optional[str]:
        if not sender_id:
            return None
        row = self.db.execute("SELECT name FROM names WHERE jid = ?", (sender_id,)).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------- catch-up points

    def cursor(self, chat: str) -> Optional[int]:
        """The newest message id queued for `chat`, or None if never set."""
        row = self.db.execute("SELECT last_id FROM cursors WHERE chat = ?", (chat,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, chat: str, last_id: int) -> None:
        """Move `chat`'s catch-up point forward (never back)."""
        self.db.execute(
            "INSERT INTO cursors (chat, last_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (chat) DO UPDATE SET last_id = MAX(cursors.last_id, excluded.last_id), "
            "updated_at = excluded.updated_at",
            (chat, int(last_id), time.time()),
        )

    # ---------------------------------------------------------- upkeep

    def prune(self) -> None:
        now = time.time()
        self.db.execute(
            "DELETE FROM inbox WHERE status != 'pending' AND received_at < ?",
            (now - DONE_RETENTION_S,),
        )
        self.db.execute("DELETE FROM msgmap WHERE created_at < ?", (now - MAP_RETENTION_S,))
