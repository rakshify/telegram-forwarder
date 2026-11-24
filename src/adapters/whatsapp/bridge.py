"""`forward` for a WhatsApp source (also `wa-forward`): a linked account's chats -> their
Telegram and Signal chats.

This only puts the pieces together: the WhatsApp source (source.py), a sink
per destination platform (src/adapters), and the pipeline with its on-disk
queue (src/core). How a message travels is described in src/core/pipeline.py.
"""
from typing import Dict, Optional

from ... import config
from ...core import model
from ...core.model import Routes
from ...core.pipeline import Pipeline
from ...core.sink import Sink
from ...core.store import Store
from .. import make_sinks
from . import accounts
from . import client as wa
from .source import WhatsAppSource


class Bridge:
    def __init__(self, account: accounts.WaAccount, cfg: Routes, dry_run: bool = False,
                 store: Optional[Store] = None, sinks: Optional[Dict[str, Sink]] = None):
        self.account = account
        self.cfg = cfg
        self.dry_run = dry_run
        # A dry run must not touch the real queue or message map.
        self.store = store or Store(":memory:" if dry_run else account.state_path)
        self.sinks = sinks or make_sinks(cfg.pairs, telegram_session=account.bot_session_base)
        self.telegram = self.sinks[model.TELEGRAM]  # also sends the alerts
        self.source = WhatsAppSource(account, [p.source for p in cfg.pairs])
        self.pipeline = Pipeline(
            source=self.source, pairs=cfg.pairs, sinks=self.sinks, store=self.store,
            title=f"WhatsApp bridge ({account.display})", alerts=cfg.alerts,
            notifier=self.telegram, dry_run=dry_run,
        )

    async def run(self) -> int:
        config.validate(require_bot=True)
        wa.quiet_logs(debug=self.source.debug)  # neonize turns logging up when imported
        mode = "  [dry run: nothing is posted]" if self.dry_run else ""
        print(f"WhatsApp bridge for {self.account.display} "
              f"(short_id={self.account.short_id}){mode}")
        return await self.pipeline.run()


async def run(routes: Routes, dry_run: bool = False) -> int:
    acct = accounts.get_account(routes.account)
    return await Bridge(acct, routes, dry_run=dry_run).run()
