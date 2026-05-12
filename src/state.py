"""Persistent forward state — last successfully-forwarded source message id per pair.

Used at startup to backfill messages that arrived in source chats while the
forwarder was offline.

State is stored per-user (`forward_state_<short_id>.json`) so multiple
forwarder containers can write concurrently without clobbering each other's
state via whole-file rewrites. Within a user's file, the key is per-pair
(per-source-and-dest, plus per-topic when topic-filtered).
"""
import json
from pathlib import Path
from typing import Dict

from . import config


def _state_path(short_id: str) -> Path:
    return config.SESSION_DIR / f"forward_state_{short_id}.json"


def state_key(short_id: str, source_id: int, dest_id: int, topic_id: int = 0) -> str:
    """Per-pair state key.

    Topic-aware pairs get a 4-part key; topic_id=0 (no topic) keeps the legacy
    3-part key so existing forward_state.json entries keep matching.
    """
    if topic_id:
        return f"{short_id}:{source_id}:{dest_id}:{topic_id}"
    return f"{short_id}:{source_id}:{dest_id}"


def load_state(short_id: str) -> Dict[str, int]:
    """Load this user's state file, falling back to the legacy shared file
    (forward_state.json) on first run after upgrading. Once migrated, the
    legacy file is no longer read.
    """
    path = _state_path(short_id)
    if path.exists():
        with path.open() as f:
            return json.load(f)

    # One-time migration: pluck this user's keys out of the legacy shared
    # file (if any) so we don't lose forward progress when upgrading.
    legacy = config.SESSION_DIR / "forward_state.json"
    if legacy.exists():
        try:
            with legacy.open() as f:
                shared = json.load(f)
        except Exception:
            return {}
        prefix = f"{short_id}:"
        mine = {k: v for k, v in shared.items() if k.startswith(prefix)}
        if mine:
            save_state(short_id, mine)
            print(f"  migrated {len(mine)} state entries from legacy "
                  f"forward_state.json to forward_state_{short_id}.json")
        return mine

    return {}


def save_state(short_id: str, state: Dict[str, int]) -> None:
    path = _state_path(short_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)
