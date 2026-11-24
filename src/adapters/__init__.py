"""One package per platform. Each turns the neutral model (src/core) into that
platform's calls, and only its own package imports the platform's library.

telegram/       source (a user account) and destination (the bot); `forward`
whatsapp/       source (a linked WhatsApp device); `forward` / `wa-forward`
signal/sink.py  destination, through signal-cli-rest-api (HTTP)

routes.py       the one config format, for every source, and running a config
dests.py        how a config names a destination (Telegram or Signal)
"""
from pathlib import Path
from typing import Dict, List, Union

from .. import config
from ..core.model import SIGNAL, TELEGRAM, Pair
from ..core.sink import Sink


def make_sinks(pairs: List[Pair], telegram_session: Union[str, Path]) -> Dict[str, Sink]:
    """A sink for each platform the pairs post to. Telegram's is always there:
    its bot also sends the alerts. Each process keeps its own bot session file."""
    from .telegram.sink import TelegramSink  # imported here: only sinks in use load their library

    sinks: Dict[str, Sink] = {TELEGRAM: TelegramSink(telegram_session)}
    if any(p.dest.platform == SIGNAL for p in pairs):
        from .signal.sink import SignalSink
        sinks[SIGNAL] = SignalSink(config.SIGNAL_API_URL, config.SIGNAL_SENDER)
    return sinks
