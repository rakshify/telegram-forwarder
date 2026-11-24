"""What a config says about WhatsApp sources (the format itself: src/adapters/routes.py).

A WhatsApp pair names its source chat like this:

    "source": "120363012345678901@g.us"   # a group id from `wa-list-groups`
    "source": "+919876543210"             # or a 1:1 chat, by phone number

A WhatsApp config, configs/wa-<short_id>.json:

{
  "platform": "whatsapp",
  "account": "a1b2c3",                    # short id from `wa-list-accounts` (or pass -a)
  "attribution": true,
  "alerts": 123456789,
  "pairs": [
    {"source": "120363012345678901@g.us",
     "dests": [{"dest": "-1004200000001", "dest_hash": 1234567890123456789},
               {"dest_platform": "signal", "dest": "group.YWJjZGVm..."}]},
    {"source": "+919876543210", "dest": "-1004200000002", "dest_hash": 987654321,
     "dest_topic": 12, "attribution": false}
  ]
}
"""
import re
from typing import Tuple

_SERVERS = {"g.us", "s.whatsapp.net", "lid"}


def normalize_source(raw) -> str:
    """Accept a WhatsApp chat id or a +phone number; return a canonical chat id."""
    s = str(raw).strip()
    if "@" in s:
        user, server = s.split("@", 1)
        server = server.lower()
        if server == "newsletter":
            raise SystemExit(
                f"source {s!r}: WhatsApp Channels aren't supported yet (groups and 1:1 chats are)."
            )
        if server not in _SERVERS or not user:
            raise SystemExit(f"source {s!r}: not a WhatsApp group or chat id.")
        if server == "s.whatsapp.net":
            user = re.sub(r"\D", "", user)
        return f"{user}@{server}"
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if 8 <= len(digits) <= 15:
            return f"{digits}@s.whatsapp.net"
    raise SystemExit(
        f"source {s!r}: use a group id like 120363012345678901@g.us (from wa-list-groups) "
        f"or a phone number with + and country code for a 1:1 chat."
    )


def parse_source(entry: dict) -> Tuple[str, str]:
    """A pair's source chat: (its key in the pipeline, which is the canonical chat id, twice)."""
    chat = normalize_source(entry["source"])
    return chat, chat
