"""How config files name a destination: the same keys for every source's config.

    {"dest": "-1004200000001", "dest_hash": 123, "dest_topic": 12}       Telegram (default)
    {"dest_platform": "signal", "dest": "group.YWJj..."}                 a Signal group
    {"dest_platform": "signal", "dest": "+15551234567"}                  a Signal 1:1 chat
"""
import re

from ..core.model import DEST_PLATFORMS, SIGNAL, TELEGRAM, Dest
from .telegram.peers import parse_chat_id


def normalize_signal_dest(raw) -> str:
    """A Signal group id (group.<base64>) or a +phone number, canonical."""
    s = str(raw).strip()
    if s.startswith("group.") and len(s) > len("group."):
        return s
    if s.startswith("+"):
        digits = re.sub(r"[\s().-]", "", s[1:])
        if digits.isdigit() and 8 <= len(digits) <= 15:
            return "+" + digits
    raise ValueError(
        f"Signal dest {s!r}: use a group id like group.YWJj... (from signal-cli-rest-api's "
        f"/v1/groups) or a phone number with + and country code."
    )


def parse_dest(entry: dict) -> Dest:
    """The destination of one config pair. Raises KeyError for a missing key
    and ValueError for a bad value; the config loader adds where it was."""
    platform = str(entry.get("dest_platform") or TELEGRAM).strip().lower()
    if platform == TELEGRAM:
        return Dest(TELEGRAM, str(parse_chat_id(str(entry["dest"]))),
                    hash=int(entry["dest_hash"]), topic=int(entry.get("dest_topic", 0)))
    if platform == SIGNAL:
        telegram_only = [k for k in ("dest_hash", "dest_topic") if k in entry]
        if telegram_only:
            raise ValueError(f"{' and '.join(telegram_only)}: only for Telegram destinations")
        return Dest(SIGNAL, normalize_signal_dest(entry["dest"]))
    raise ValueError(f"dest_platform {platform!r}: use one of {', '.join(DEST_PLATFORMS)}")
