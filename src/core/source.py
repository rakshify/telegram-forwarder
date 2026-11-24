"""The interface a source (where messages come from) implements, and what the
pipeline offers it in return (Host).

A source connects to its platform and, for each message from a configured
chat, writes its own payload to the pipeline's queue (Host.enqueue). Later
the pipeline asks it to turn a queued payload into a neutral Message
(decode) and to fetch that message's media (download). Everything else,
posting, retries, the message map and alerts, is the pipeline's.
"""
from pathlib import Path
from typing import Optional, Protocol, Union

from .model import Message, Skip
from .store import InboxItem, Store


class NotConnected(Exception):
    """Media can only be fetched while the source is connected. Not a
    failure: the pipeline waits for the source to reconnect, then retries."""


class MediaUnavailable(Exception):
    """The source no longer serves this media (expired or removed)."""


class Host(Protocol):
    """What the pipeline offers the source it runs."""
    store: Store    # for the names table
    dry_run: bool

    @property
    def stopping(self) -> bool: ...

    def enqueue(self, chat: str, msg_id: str, payload: bytes, digest: str = "") -> bool:
        """Queue a message for posting; False if this exact one was queued
        already. Call it before awaiting anything, so the queue keeps the
        platform's order."""

    def wake(self) -> None:
        """Have the worker look at the queue again (e.g. after reconnecting)."""

    def dests_label(self, chat: str) -> str:
        """A source chat's destinations, for log lines."""

    async def alert(self, key: str, text: str) -> None:
        """Log `text`, and tell whoever gets alerts (at most hourly per `key`)."""

    async def sleep(self, seconds: float) -> None:
        """Sleep, but return early when stopping."""

    def request_stop(self, exit_code: Optional[int] = None) -> None: ...


class Source(Protocol):
    name: str        # "WhatsApp": for log lines, alerts and notes
    connected: bool  # media can only be fetched while connected

    def attach(self, host: Host) -> None:
        """Called once, by the pipeline that will run this source."""

    async def start(self) -> None:
        """Connect, and start queuing messages from the configured chats."""

    async def stop(self) -> None: ...

    def decode(self, item: InboxItem) -> Union[Message, Skip]:
        """A queued payload -> what to post. Raises if it can't be read."""

    async def download(self, msg: Message, folder: Path) -> Path:
        """Fetch `msg.media` into `folder`. Raises NotConnected or MediaUnavailable."""

    def label(self, chat: str) -> str:
        """A source chat's name, for log lines."""

    async def check(self) -> None:
        """Called by the watchdog about every 30 seconds, e.g. to alert after
        a long disconnect."""
