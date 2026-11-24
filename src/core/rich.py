"""Text with formatting, independent of any platform.

`Rich` is plain text plus `Span`s: typed ranges such as bold or quote.
Sources parse their own markup into it (WhatsApp's *bold*, in
src/adapters/whatsapp/formatting.py) and each sink renders it its own way (Telegram
message entities, in src/adapters/telegram/sink.py).

Offsets
-------
Spans use code-point offsets (Python string indexes) so the code stays
simple. Length limits (`split`, `truncate`) are measured in UTF-16 code
units instead: that is how Telegram counts (emoji and other characters
outside the Basic Multilingual Plane count as 2), and it is a safe upper
bound for platforms that count characters.
"""
import bisect
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# Styles
BOLD = "bold"
ITALIC = "italic"
UNDERLINE = "underline"
STRIKE = "strike"
SPOILER = "spoiler"
CODE = "code"
PRE = "pre"                    # data: the code's language, if any
QUOTE = "quote"                # data: "collapsed" for a collapsed blockquote
# Links and references
TEXT_URL = "text_url"          # data: the URL behind the text
URL = "url"
EMAIL = "email"
PHONE = "phone"
MENTION = "mention"            # @username
MENTION_NAME = "mention_name"  # data: the mentioned user's id (Telegram)
HASHTAG = "hashtag"
CASHTAG = "cashtag"
BOT_COMMAND = "bot_command"
BANK_CARD = "bank_card"
CUSTOM_EMOJI = "custom_emoji"  # data: the emoji's document id (Telegram)
RAW = "raw"                    # data: "<platform>:<blob>", a platform's own span kept as is

# Spans that stand for one thing each: two of them side by side stay two.
ATOMIC = frozenset({URL, EMAIL, PHONE, MENTION, MENTION_NAME, HASHTAG, CASHTAG,
                    BOT_COMMAND, BANK_CARD, CUSTOM_EMOJI, RAW})


@dataclass(frozen=True)
class Span:
    kind: str
    start: int      # code-point offset, inclusive
    end: int        # code-point offset, exclusive
    data: str = ""  # what some kinds carry (see above)


@dataclass
class Rich:
    """Plain text plus formatting spans (code-point offsets)."""
    text: str = ""
    spans: List[Span] = field(default_factory=list)


# ------------------------------------------------------------------ utf-16

def _units(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def utf16_len(text: str) -> int:
    return sum(_units(ch) for ch in text)


def utf16_offsets(text: str) -> List[int]:
    """UTF-16 offset of every code-point index of `text`, plus one for its end."""
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + _units(ch))
    return offsets


def from_utf16(offsets: List[int], unit: int) -> int:
    """The code-point index at UTF-16 offset `unit`, given utf16_offsets() of
    the text. An offset inside a character counts as that character's start."""
    return max(0, bisect.bisect_right(offsets, unit) - 1)


# ------------------------------------------------------------------ building

def plain(text: str) -> Rich:
    return Rich(text, [])


def styled(text: str, *kinds: str) -> Rich:
    """`text` with each of `kinds` applied to all of it."""
    if not text:
        return Rich("", [])
    return Rich(text, [Span(k, 0, len(text)) for k in kinds])


def concat(*parts: Rich) -> Rich:
    text: List[str] = []
    spans: List[Span] = []
    pos = 0
    for part in parts:
        if part is None or not part.text:
            continue
        text.append(part.text)
        spans.extend(Span(s.kind, s.start + pos, s.end + pos, s.data) for s in part.spans)
        pos += len(part.text)
    return Rich("".join(text), normalize(spans, pos))


def normalize(spans: Iterable[Span], length: int) -> List[Span]:
    """Clip to [0, length] and drop empty spans. Touching or overlapping spans
    of the same style (same kind and data) merge; ATOMIC ones never do."""
    clipped = sorted(
        (Span(s.kind, max(0, s.start), min(length, s.end), s.data) for s in spans),
        key=lambda s: (s.kind, s.data, s.start, s.end),
    )
    merged: List[Span] = []
    for s in clipped:
        if s.end <= s.start:
            continue
        last = merged[-1] if merged else None
        if (last is not None and last.kind == s.kind and last.data == s.data
                and s.kind not in ATOMIC and s.start <= last.end):
            merged[-1] = Span(s.kind, last.start, max(last.end, s.end), s.data)
        else:
            merged.append(s)
    merged.sort(key=lambda s: (s.start, -s.end, s.kind, s.data))
    return merged


def slice_rich(rich: Rich, start: int, end: int) -> Rich:
    """Characters [start, end) of `rich`, spans clipped and rebased."""
    shifted = [Span(s.kind, s.start - start, s.end - start, s.data) for s in rich.spans]
    return Rich(rich.text[start:end], normalize(shifted, end - start))


def strip(rich: Rich) -> Rich:
    """Trim leading/trailing whitespace (Telegram trims it too, which would
    otherwise shift every span after it)."""
    text = rich.text
    lead = len(text) - len(text.lstrip())
    end = len(text.rstrip())
    if lead >= end:
        return Rich("", [])
    return slice_rich(rich, lead, end)


def replace_ranges(rich: Rich, replacements: Sequence[Tuple[int, int, str]]) -> Rich:
    """Replace text ranges [start, end) with new strings, keeping spans aligned.

    A span that covers a replaced range grows or shrinks with it; a span
    that starts or ends inside one snaps to the replacement's edge.
    """
    text = rich.text
    spans = list(rich.spans)
    for start, end, new in sorted(replacements, key=lambda r: r[0], reverse=True):
        delta = len(new) - (end - start)
        text = text[:start] + new + text[end:]
        adjusted = []
        for s in spans:
            a, b = s.start, s.end
            if a >= end:
                a += delta
            elif a > start:
                a = start
            if b >= end:
                b += delta
            elif b > start:
                b = start + len(new)
            adjusted.append(Span(s.kind, a, b, s.data))
        spans = adjusted
    return Rich(text, normalize(spans, len(text)))


def truncate(rich: Rich, limit: int, ellipsis: str = "…") -> Rich:
    """Cut `rich` to at most `limit` UTF-16 units, ending in `ellipsis` if cut."""
    if utf16_len(rich.text) <= limit:
        return rich
    budget = limit - utf16_len(ellipsis)
    end = _fit(rich.text, 0, budget)
    return concat(strip(slice_rich(rich, 0, end)), plain(ellipsis))


def _fit(text: str, start: int, limit: int) -> int:
    """Largest end such that text[start:end] is at most `limit` UTF-16 units."""
    units = 0
    end = start
    while end < len(text):
        u = _units(text[end])
        if units + u > limit:
            break
        units += u
        end += 1
    return end


def split(rich: Rich, first_limit: int, rest_limit: Optional[int] = None) -> List[Rich]:
    """Split into chunks that fit a platform's limits (in UTF-16 units).

    The first chunk fits `first_limit` (e.g. Telegram's 1024 for a media
    caption), the rest fit `rest_limit` (4096 for its text messages). Cuts
    prefer a line break, then a space, in the second half of the window;
    otherwise the text is cut at the limit.
    """
    rest_limit = rest_limit or first_limit
    text = rich.text
    chunks: List[Rich] = []
    start = 0
    limit = first_limit
    while start < len(text):
        end = _fit(text, start, limit)
        if end < len(text):
            floor = start + (end - start) // 2
            cut = text.rfind("\n", floor, end)
            if cut < 0:
                cut = text.rfind(" ", floor, end)
            if cut >= 0:
                end = cut + 1
        if end <= start:  # a single character wider than the limit; can't happen for limit >= 2
            end = start + 1
        chunk = strip(slice_rich(rich, start, end))
        if chunk.text:
            chunks.append(chunk)
        start = end
        limit = rest_limit
    return chunks
