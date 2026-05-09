"""Configuration loaded from environment variables.

Loads `.env` automatically when present (useful for local dev outside Docker).
"""
import os
import sys
from pathlib import Path

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
