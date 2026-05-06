"""Persistent forward state — last successfully-forwarded source message id per pair.

Used at startup to backfill messages that arrived in source chats while the
forwarder was offline.

Key shape: "<short_id>:<source_id>:<dest_id>" — per-pair (not per-source), so
that running the same source to two different destinations doesn't cause one
pair to skip messages the other already saw.
"""
import json
from pathlib import Path
from typing import Dict

from . import config


def _state_path() -> Path:
    return config.SESSION_DIR / "forward_state.json"


def state_key(short_id: str, source_id: int, dest_id: int) -> str:
    return f"{short_id}:{source_id}:{dest_id}"


def load_state() -> Dict[str, int]:
    path = _state_path()
    if not path.exists():
        return {}
    with path.open() as f:
        return json.load(f)


def save_state(state: Dict[str, int]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(path)
