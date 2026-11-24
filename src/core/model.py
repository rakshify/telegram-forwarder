"""What gets posted, and where to.

Sources turn their platform's messages into `Message`s; the pipeline posts
each one to every `Pair` of its chat, through the sink for the pair's `Dest`.
A config file loads into `Routes`: one source account and its pairs.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .rich import Rich

TELEGRAM = "telegram"
SIGNAL = "signal"
WHATSAPP = "whatsapp"
SOURCE_PLATFORMS = (TELEGRAM, WHATSAPP)  # where messages can come from
DEST_PLATFORMS = (TELEGRAM, SIGNAL)      # where pairs can post

_NAMES = {TELEGRAM: "Telegram", SIGNAL: "Signal", WHATSAPP: "WhatsApp"}


@dataclass
class Dest:
    """One destination chat, on some platform."""
    platform: str        # TELEGRAM or SIGNAL
    chat: str            # Telegram: bare chat id (no -100). Signal: +number or group.<id>
    hash: int = 0        # Telegram: access hash; 0 only for an old-style basic group
    topic: int = 0       # Telegram: forum topic id; 0 for none
    handle: Optional[object] = None  # what the sink resolved it to; None while unreachable

    @property
    def label(self) -> str:
        """Short form for log lines, e.g. 1004200000001#12 or signal:+15551234567."""
        if self.platform == TELEGRAM:
            return f"{self.chat}#{self.topic}" if self.topic else self.chat
        return f"{self.platform}:{self.chat}"

    @property
    def title(self) -> str:
        """For messages to people, e.g. "Telegram chat 1004200000001"."""
        name = _NAMES.get(self.platform, self.platform)
        if self.platform == TELEGRAM:
            return f"{name} chat {self.label}"
        kind = "group" if self.chat.startswith("group.") else "chat"
        return f"{name} {kind} {self.chat}"


@dataclass
class Media:
    """An attachment, described well enough for a sink to post it the right way."""
    kind: str              # photo | video | video_note (round) | animation | voice | audio
                           # | document | sticker
    mimetype: str = ""
    filename: str = ""     # original file name (documents)
    seconds: int = 0
    width: int = 0
    height: int = 0
    waveform: bytes = b""  # voice notes: amplitude samples, 0-100 each
    animated: bool = False  # stickers
    title: str = ""        # audio
    performer: str = ""    # audio


@dataclass
class Poll:
    question: str
    options: List[str]
    multiple: bool
    quiz: bool = False
    correct: Optional[int] = None    # quiz: index of the right option, if known
    public: bool = False             # voters are visible
    close_period: int = 0            # closes this many seconds after posting
    close_date: Optional[int] = None  # or at this Unix time


@dataclass
class Pair:
    """One source chat -> one destination."""
    source: str           # the source's chat id, e.g. 1203630...@g.us
    dest: Dest
    attribution: bool = False  # prefix messages with the sender's name

    @property
    def key(self) -> str:
        """Identifies the pair in the message map (store.msgmap)."""
        d = self.dest
        if d.platform == TELEGRAM:
            return f"{self.source}>{d.chat}:{d.topic}"  # the format from before other platforms
        return f"{self.source}>{d.platform}:{d.chat}"

    @property
    def label(self) -> str:
        return f"{self.source} -> {self.dest.label}"


@dataclass
class Quote:
    """The message a message replies to."""
    msg_id: str            # the source's id of it
    sender_name: str = ""  # who wrote it, if known
    preview: Rich = field(default_factory=Rich)  # short preview, for when it was never forwarded


@dataclass
class Message:
    """One message from a source, as the pipeline posts it."""
    id: str                # the source's id: what the message map is keyed by
    kind: str              # text | media | poll | edit
    label: str             # short description for logs: "text", "photo", ...
    sender_name: str = ""  # shown on pairs with attribution
    body: Rich = field(default_factory=Rich)  # text or caption; for an edit, the new text
    media: Optional[Media] = None
    poll: Optional[Poll] = None
    reply: Optional[Quote] = None
    edit_of: str = ""      # kind "edit": id of the edited message


@dataclass
class Skip:
    """A queued message with nothing to post, and why."""
    reason: str
    quiet: bool = False    # not worth a log line


@dataclass
class Routes:
    """What one forwarder process runs: a source account and its pairs.
    Every config file loads into this (src/adapters/routes.py)."""
    platform: str                    # the source's platform: TELEGRAM or WHATSAPP
    account: str                     # its short id (a Telegram user, a WhatsApp account)
    pairs: List[Pair] = field(default_factory=list)
    sources: Dict[str, object] = field(default_factory=dict)  # Pair.source -> the source's own
                                                              # description of that chat
    alerts: Optional[int] = None     # a Telegram user id the bot alerts

    def add(self, source: str, spec: object, dest: Dest, attribution: bool) -> bool:
        """Add a pair. False (with a warning) if the same pair is there already."""
        pair = Pair(source=source, dest=dest, attribution=attribution)
        if any(p.key == pair.key for p in self.pairs):
            print(f"  WARNING: {pair.label} is listed twice; using it once.")
            return False
        self.pairs.append(pair)
        self.sources.setdefault(source, spec)
        return True
