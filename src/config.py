"""Configuration loaded from environment variables.

Loads `.env` automatically when present (useful for local dev outside Docker).
"""
import os
import sqlite3
import sys
from pathlib import Path
from typing import Union

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is optional — env vars set by docker-compose still work
    pass


TG_API_ID = os.environ.get("TG_API_ID")
TG_API_HASH = os.environ.get("TG_API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SESSION_DIR = Path(os.environ.get("SESSION_DIR", "/data/sessions"))


def bot_session_path(short_id: str) -> Path:
    """Per-user bot session file.

    The bot is the same bot (same token) regardless of which user the
    forwarder serves, but each forwarder process keeps its own local
    Telethon session SQLite. Sharing one file across multiple containers
    causes 'database is locked' errors because SQLite only tolerates one
    writer per file. Per-user files give each forwarder process exclusive
    write access.
    """
    return SESSION_DIR / f"bot_session_{short_id}"


def _prepare_session_sqlite(session_base: Union[str, Path]) -> None:
    """Enable WAL mode and a generous busy_timeout on the SQLite session.

    Without this, running `list-groups` (or any other one-off command) for a
    user whose forwarder container is already running fails with
    `sqlite3.OperationalError: database is locked` — both processes try to
    open the same .session file and SQLite's default rollback journal mode
    only allows one writer at a time.

    * WAL mode: writers don't block readers and vice versa. Persistent —
      stored in the file header once and respected by every future open.
    * busy_timeout=5000: when a write does conflict (rare in WAL), wait up
      to 5 seconds for the other writer to commit before giving up.

    Both pragmas are no-ops if the file is already in the right state, so
    calling this on every client construction is cheap.
    """
    session_file = Path(f"{session_base}.session")
    if not session_file.exists():
        # Nothing to do; the session DB hasn't been created yet. Telethon
        # will create it on connect, and the next time make_client is called
        # we'll apply the pragmas.
        return
    try:
        conn = sqlite3.connect(str(session_file), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Another process is holding an exclusive lock right now. Skip — the
        # WAL setting is persistent, so whichever process gets in first will
        # set it for everyone. Worst case, this run still fails; the next
        # one will succeed.
        pass


def make_client(session_base: Union[str, Path]):
    """Build a TelegramClient against a session file, with SQLite tuned for
    concurrent access. Use this everywhere instead of TelegramClient(...)
    directly.
    """
    # Import here to avoid a circular import: forwarder.py and others import
    # config, and importing telethon at module load time slows everything
    # down (test runs etc.).
    from telethon import TelegramClient

    _prepare_session_sqlite(session_base)
    return TelegramClient(str(session_base), api_id_int(), TG_API_HASH)


def validate(require_bot: bool = True) -> None:
    """Validate that required env vars are set. Exit with a friendly error otherwise."""
    missing = []
    if not TG_API_ID:
        missing.append("TG_API_ID")
    if not TG_API_HASH:
        missing.append("TG_API_HASH")
    if require_bot and not BOT_TOKEN:
        missing.append("BOT_TOKEN")

    if missing:
        print(
            f"Missing required environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)


def api_id_int() -> int:
    return int(TG_API_ID)
