"""One config format for every source, and running what a config describes.

configs/<name>.json:

{
  "platform": "telegram",      # where messages come from: "telegram" or "whatsapp"
  "account": "abc123",         # whose session reads them: a short id from `list-users`
                               # (Telegram) or `wa-list-accounts` (WhatsApp); or pass -u / -a
  "attribution": false,        # optional: prefix messages with the sender's name
  "alerts": 123456789,         # optional: a Telegram user id the bot alerts
  "pairs": [
    { <source fields>, <one destination> },
    { <source fields>, "dests": [<destination>, <destination>, ...] }
  ],
  "auto": [ ... ]              # Telegram only: mirror a community's topics (telegram/config_file.py)
}

Source fields, by platform:
  telegram   "source": "-1001234567890", "source_hash": 1234..., "topic": 17 (optional)
  whatsapp   "source": "120363012345678901@g.us", or "+919876543210" for a 1:1 chat

A destination (dests.py), on any platform:
  {"dest": "-1005555555555", "dest_hash": 0, "dest_topic": 5}    Telegram (the default)
  {"dest_platform": "signal", "dest": "group.YWJj..."}            Signal; "+1555..." for a 1:1 chat
Each can also set "attribution": a pair's setting is the default for its
destinations, and the file's for its pairs. Keys starting with "_" are comments.

Files from before this format load as they always did: "user" marks a
Telegram config, and "account" with WhatsApp sources (…@g.us, +phone) a
WhatsApp one. Either way the pairs get the same keys as before, so the
state that refers to them (queues, message maps, catch-up points) carries on.
"""
import json
from pathlib import Path
from typing import Callable, Optional, Tuple

from ..core.model import SOURCE_PLATFORMS, TELEGRAM, WHATSAPP, Routes
from .dests import parse_dest

_DEST_KEYS = ("dest", "dest_hash", "dest_topic", "dest_platform")

SourceParser = Callable[[dict], Tuple[str, object]]  # pair entry -> (Pair.source, spec)


def platform_of(data: dict, given: Optional[str] = None) -> str:
    """The source platform a config is for. `given` is the command's (wa-forward: WhatsApp)."""
    named = str(data.get("platform") or "").strip().lower() or None
    if named and given and named != given:
        raise ValueError(f"this config is for {named} (\"platform\"), not {given}")
    platform = named or given or _guess(data)
    if platform not in SOURCE_PLATFORMS:
        raise ValueError(f"platform {platform!r}: use one of {', '.join(SOURCE_PLATFORMS)}")
    return platform


def _guess(data: dict) -> str:
    """For files without "platform": what the formats from before looked like."""
    pairs = [p for p in data.get("pairs") or [] if isinstance(p, dict)]
    if "user" in data or data.get("auto") or any("source_hash" in p for p in pairs):
        return TELEGRAM
    if any(str(p.get("source", "")).strip().startswith("+") or "@" in str(p.get("source", ""))
           for p in pairs):
        return WHATSAPP
    return WHATSAPP if "account" in data else TELEGRAM


def _account(data: dict, fallback: Optional[str], platform: str) -> str:
    named = {str(data[k]).strip() for k in ("account", "user") if data.get(k)}
    if len(named) > 1:
        raise ValueError("'account' and 'user' name different accounts; use 'account'")
    account = named.pop() if named else (fallback or "").strip()
    if not account:
        flag = "-a" if platform == WHATSAPP else "-u"
        raise ValueError(f"no 'account' in the config and no {flag} given")
    if platform == WHATSAPP and account.startswith("wa-"):
        account = account[3:]  # wa-<short_id>, as forwarder.sh names them
    return account


def _alerts(raw) -> Optional[int]:
    if raw in (None, "", 0):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError("'alerts' must be a numeric Telegram user/chat id")


def _source_parser(platform: str) -> SourceParser:
    if platform == TELEGRAM:
        from .telegram.config_file import parse_source
    else:
        from .whatsapp.config_file import parse_source
    return parse_source


async def load(path: Path, platform: Optional[str] = None, account: Optional[str] = None) -> Routes:
    """Read a config file. `platform` and `account` come from the command line:
    the command that ran (wa-forward means WhatsApp) and its -u / -a."""
    try:
        with path.open() as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path}: not valid JSON ({e})")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    try:
        kind = platform_of(data, platform)
        routes = Routes(platform=kind, account=_account(data, account, kind),
                        alerts=_alerts(data.get("alerts")))
    except ValueError as e:
        raise SystemExit(f"{path}: {e}")

    parse_source = _source_parser(kind)
    default_attribution = bool(data.get("attribution", False))
    for i, entry in enumerate(data.get("pairs") or []):
        add_pair(routes, entry, parse_source, default_attribution, f"{path}: pairs[{i}]")

    if data.get("auto"):
        if kind != TELEGRAM:
            raise SystemExit(f"{path}: 'auto' blocks are for Telegram sources only.")
        from .telegram.config_file import expand_auto
        await expand_auto(routes, data["auto"], path, default_attribution)

    if not routes.pairs:
        need = " (need 'pairs' or 'auto')" if kind == TELEGRAM else ""
        raise SystemExit(f"{path}: no pairs defined{need}.")
    return routes


def add_pair(routes: Routes, entry: dict, parse_source: SourceParser, default_attribution: bool,
             where: str) -> None:
    """One `pairs` entry: its source, and the destination or `dests` it posts to."""
    try:
        source, spec = parse_source(entry)
        attribution = bool(entry.get("attribution", default_attribution))
        if "dests" in entry:
            if any(k in entry for k in _DEST_KEYS):
                raise ValueError("give either 'dests' or one destination ('dest', ...), not both")
            items, label = entry["dests"], ".dests[{}]"
            if not isinstance(items, list) or not items:
                raise ValueError("'dests' must be a list of one or more destinations")
        else:
            items, label = [entry], ""
    except KeyError as e:
        raise SystemExit(f"{where} missing key {e}")
    except ValueError as e:
        raise SystemExit(f"{where}: {e}")

    for j, item in enumerate(items):
        here = where + label.format(j)
        try:
            if not isinstance(item, dict):
                raise ValueError("a destination is an object like {\"dest\": ..., \"dest_hash\": ...}")
            dest = parse_dest(item)
        except KeyError as e:
            raise SystemExit(f"{here} missing key {e}")
        except ValueError as e:
            raise SystemExit(f"{here}: {e}")
        routes.add(source, spec, dest, bool(item.get("attribution", attribution)))


async def run(routes: Routes, dry_run: bool = False) -> int:
    """Run the forwarder a config describes. Each platform's code is imported
    only when used, so a Telegram forwarder never loads neonize."""
    if routes.platform == WHATSAPP:
        from .whatsapp import bridge
        return await bridge.run(routes, dry_run=dry_run)
    from .telegram import forward
    return await forward.run(routes, dry_run=dry_run)
