"""Linked WhatsApp accounts: registry, short ids and per-account files.

Mirrors users.py on the Telegram side. Each linked account gets a
deterministic 6-character short id derived from its phone number; that id
names its config (configs/wa-<short_id>.json), its container
(tg-forwarder-wa-<short_id>) and its files in SESSION_DIR:

    wa_accounts.json                   registry of linked accounts
    wa_<short_id>.db                   WhatsApp session (keys). As sensitive
                                       as the phone's WhatsApp: whoever has
                                       it can read the account's messages.
    wa_state_<short_id>.sqlite         queue + message-id map (store.py)
    bot_session_wa-<short_id>.session  the bridge's own Telethon bot session
"""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

from ... import config


def normalize_phone(raw: str) -> str:
    """'+91 98765-43210' -> '919876543210'.

    The leading + (or 00) is required: pairing sends a prompt to whichever
    WhatsApp account owns the number, so the country code must be explicit
    rather than guessed.
    """
    s = (raw or "").strip()
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        raise SystemExit(
            f"{raw!r}: start the number with + and the country code, e.g. +919876543210"
        )
    digits = re.sub(r"\D", "", s)
    if not 8 <= len(digits) <= 15:
        raise SystemExit(f"{raw!r} doesn't look like a full international phone number.")
    return digits


def make_short_id(phone_digits: str) -> str:
    return hashlib.sha256(f"wa:{phone_digits}".encode()).hexdigest()[:6]


@dataclass
class WaAccount:
    short_id: str
    phone: str                       # digits with country code, no +
    jid: Optional[str] = None        # e.g. 919876543210@s.whatsapp.net
    push_name: Optional[str] = None  # WhatsApp profile name at link time
    linked_at: Optional[str] = None  # ISO timestamp

    @property
    def db_path(self) -> Path:
        return config.SESSION_DIR / f"wa_{self.short_id}.db"

    @property
    def state_path(self) -> Path:
        return config.SESSION_DIR / f"wa_state_{self.short_id}.sqlite"

    @property
    def bot_session_base(self) -> Path:
        # Own bot session per bridge process: two processes sharing one
        # Telethon session file crash with "database is locked".
        return config.bot_session_path(f"wa-{self.short_id}")

    @property
    def display(self) -> str:
        name = self.push_name or "(no name)"
        return f"{name} +{self.phone}"


def _db_path() -> Path:
    return config.SESSION_DIR / "wa_accounts.json"


def load_db() -> Dict[str, WaAccount]:
    path = _db_path()
    if not path.exists():
        return {}
    with path.open() as f:
        raw = json.load(f)
    return {sid: WaAccount(**rec) for sid, rec in raw.items()}


def save_db(db: Dict[str, WaAccount]) -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump({sid: asdict(rec) for sid, rec in db.items()}, f, indent=2)
    tmp.replace(path)


def get_account(short_id: str) -> WaAccount:
    short_id = short_id[3:] if short_id.startswith("wa-") else short_id
    db = load_db()
    if short_id not in db:
        raise SystemExit(
            f"No linked WhatsApp account with short id '{short_id}'. "
            f"Run `wa-list-accounts`, or link one with `wa-link`."
        )
    return db[short_id]


def upsert(account: WaAccount) -> None:
    db = load_db()
    db[account.short_id] = account
    save_db(db)


def remove(short_id: str) -> Optional[WaAccount]:
    db = load_db()
    acct = db.pop(short_id, None)
    if acct is not None:
        save_db(db)
    return acct


def session_files(account: WaAccount):
    """Every file belonging to the account (for unlink / re-link)."""
    files = []
    for base in (account.db_path, account.state_path):
        files += [base, Path(f"{base}-wal"), Path(f"{base}-shm"), Path(f"{base}-journal")]
    bot = account.bot_session_base
    files += [Path(f"{bot}.session"), Path(f"{bot}.session-journal"),
              Path(f"{bot}.session-wal"), Path(f"{bot}.session-shm")]
    return files
