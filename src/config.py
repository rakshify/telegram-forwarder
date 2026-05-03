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

USER_SESSION_PATH = SESSION_DIR / "user_session"
BOT_SESSION_PATH = SESSION_DIR / "bot_session"


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
