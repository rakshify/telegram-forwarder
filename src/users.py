"""Multi-user session management.

Each Telegram user account that logs in gets a record in users.json,
identified by a deterministic 6-char hex short_id derived from the user's
numeric Telegram user id. The short_id is what humans pass on the CLI via
`-u`; the actual session file is named `user_<short_id>.session`.
"""
import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from . import config


def _users_db_path() -> Path:
    return config.SESSION_DIR / "users.json"


def make_short_id(user_id: int) -> str:
    """Deterministic 6-char hex id derived from the Telegram user id.

    SHA-256 truncated to 6 hex chars gives ~16M slots — collision risk is
    negligible for any reasonable number of accounts on one host.
    """
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:6]


@dataclass
class UserRecord:
    short_id: str
    user_id: int
    first_name: Optional[str]
    last_name: Optional[str]
    username: Optional[str]
    phone: Optional[str]

    @property
    def session_base(self) -> Path:
        # Pass this (without extension) to TelegramClient — Telethon adds .session
        return config.SESSION_DIR / f"user_{self.short_id}"

    @property
    def session_filename(self) -> str:
        return f"user_{self.short_id}.session"

    @property
    def display_name(self) -> str:
        parts = [self.first_name, self.last_name]
        name = " ".join(p for p in parts if p) or "(no name)"
        if self.username:
            name += f" @{self.username}"
        return name


def load_db() -> Dict[str, UserRecord]:
    path = _users_db_path()
    if not path.exists():
        return {}
    with path.open() as f:
        raw = json.load(f)
    return {sid: UserRecord(**rec) for sid, rec in raw.items()}


def save_db(db: Dict[str, UserRecord]) -> None:
    path = _users_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump({sid: asdict(rec) for sid, rec in db.items()}, f, indent=2)
    tmp.replace(path)


def get_user(short_id: str) -> UserRecord:
    db = load_db()
    if short_id not in db:
        raise SystemExit(
            f"No user with short id '{short_id}'. "
            f"Run `list-users` to see available users."
        )
    return db[short_id]


def upsert_user(record: UserRecord) -> None:
    db = load_db()
    db[record.short_id] = record
    save_db(db)


def remove_user(short_id: str) -> Optional[UserRecord]:
    db = load_db()
    rec = db.pop(short_id, None)
    if rec is not None:
        save_db(db)
    return rec


def cleanup_session_files(base: Path) -> None:
    """Remove `<base>.session` and `<base>.session-journal` if they exist."""
    for suffix in (".session", ".session-journal"):
        p = base.with_suffix(suffix)
        if p.exists():
            p.unlink()
