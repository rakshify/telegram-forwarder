"""The interface each destination platform implements, and the errors it raises.

The pipeline (src/core/pipeline.py) decides *what* to post: it composes
the text, applies attribution and picks the message to reply to. A sink
decides *how*: the platform's limits, formatting, upload calls and error
codes. Each sink translates its platform's errors into the ones below, so
the pipeline handles every platform the same way:

    Unreachable   the destination can't be posted to right now (wrong id,
                  bot not in the chat, service down). The pipeline pauses
                  that destination, keeps the others going, and checks it
                  again every minute.
    RateLimited   wait `seconds`, then try again.
    Rejected      the platform refused this message. Tried once more if
                  `retry_once`, otherwise given up and alerted.
    CantEdit      an edit that can't be applied (too old, not supported).

Any other exception counts as transient and is retried with backoff.

Message ids are ints on every platform so far: Telegram's message id, and
Signal's timestamp (which is how Signal refers to a message).
"""
from pathlib import Path
from typing import Optional, Protocol

from .model import Dest, Media, Poll
from .rich import Rich


class SinkError(Exception):
    def __init__(self, message: str, platform: str = ""):
        super().__init__(message)
        self.platform = platform  # "Telegram", "Signal": for log lines and alerts


class Unreachable(SinkError):
    def __init__(self, message: str, platform: str = "", hint: str = ""):
        super().__init__(message, platform)
        self.hint = hint  # what to do about it, for the alert


class RateLimited(SinkError):
    def __init__(self, seconds: int, platform: str = ""):
        super().__init__(f"rate limited for {seconds}s", platform)
        self.seconds = seconds


class Rejected(SinkError):
    def __init__(self, message: str, platform: str = "", retry_once: bool = False):
        super().__init__(message, platform)
        self.retry_once = retry_once


class CantEdit(SinkError):
    pass


class Sink(Protocol):
    name: str  # "Telegram", "Signal"

    async def start(self) -> None:
        """Connect. Raises SystemExit for missing configuration."""

    async def stop(self) -> None:
        """Disconnect, if connected."""

    async def resolve(self, dest: Dest) -> object:
        """Check that `dest` can be posted to and return what to keep in
        `dest.handle`. Raises Unreachable if it can't be."""

    async def send_text(self, dest: Dest, text: Rich, reply_to: Optional[int]) -> Optional[int]:
        """Post text, split if it's too long. Returns the id of the first
        message, or None if there was nothing to post."""

    async def send_media(self, dest: Dest, media: Media, caption: Rich, path: Path,
                         reply_to: Optional[int]) -> Optional[int]:
        """Post the file at `path` with its caption. Returns its message id."""

    async def send_poll(self, dest: Dest, poll: Poll, reply_to: Optional[int]) -> Optional[int]:
        """Post a native poll. None if the platform can't hold this one; the
        pipeline then posts it as text."""

    async def edit(self, dest: Dest, msg_id: int, text: Rich, is_media: bool) -> bool:
        """Replace the text (or the caption, if `is_media`) of a message posted
        earlier. False if nothing changed. Raises CantEdit if it can't be done."""


class Notifier(Protocol):
    """Sends alerts to a person (the Telegram sink does, through the bot)."""
    ready: bool  # started, so it can send

    async def notify(self, user_id: int, text: str) -> None: ...
