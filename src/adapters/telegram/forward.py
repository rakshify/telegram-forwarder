"""`forward` for a Telegram source: one user's chats -> their Telegram and Signal chats.

This only puts the pieces together: the Telegram source (source.py), a sink
per destination platform (src/adapters), and the pipeline with its on-disk
queue, SESSION_DIR/tg_state_<short_id>.sqlite (src/core). How a message
travels is described in src/core/pipeline.py.
"""
from pathlib import Path

from ... import config, users
from ...core.model import TELEGRAM, Routes
from ...core.pipeline import Pipeline
from ...core.store import Store
from .. import make_sinks
from .source import TelegramSource


def state_path(short_id: str) -> Path:
    return config.SESSION_DIR / f"tg_state_{short_id}.sqlite"


def build(routes: Routes, user: users.UserRecord, store: Store, sinks=None,
          dry_run: bool = False) -> Pipeline:
    sinks = sinks or make_sinks(routes.pairs, telegram_session=config.bot_session_path(user.short_id))
    source = TelegramSource(user, list(routes.sources.values()))
    return Pipeline(source=source, pairs=routes.pairs, sinks=sinks, store=store,
                    title=f"Telegram forwarder ({user.display_name})", alerts=routes.alerts,
                    notifier=sinks[TELEGRAM], dry_run=dry_run)


async def run(routes: Routes, dry_run: bool = False) -> int:
    config.validate(require_bot=True)
    user = users.get_user(routes.account)
    # A dry run must not touch the real queue, message map or catch-up points.
    store = Store(":memory:" if dry_run else state_path(user.short_id))
    pipeline = build(routes, user, store, dry_run=dry_run)
    mode = "  [dry run: nothing is posted]" if dry_run else ""
    print(f"Telegram forwarder for {user.display_name} (short_id={user.short_id}){mode}")
    return await pipeline.run()
