"""WhatsApp text formatting -> Rich text (src/core/rich.py).

WhatsApp writes formatting into the text itself:

    *bold*   _italic_   ~strikethrough~   `inline code`   ```monospace```
    > quote            (at the start of a line)

`parse_whatsapp()` turns that into plain text plus spans. Each destination's
sink then renders the spans its own way (src/adapters).
"""
import re
from typing import List, Optional, Tuple

from ...core.rich import BOLD, CODE, ITALIC, PRE, QUOTE, STRIKE, Rich, Span, normalize

_MARKERS = {"*": BOLD, "_": ITALIC, "~": STRIKE}

# ```monospace``` may span lines; its content is taken verbatim.
_CODE_BLOCK_RE = re.compile(r"```(.+?)```", re.DOTALL)
# `inline code` stays on one line.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Links are kept intact: markers inside them are not formatting
# (e.g. the underscores in https://example.com/a_b_c).
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.IGNORECASE)


def parse_whatsapp(text: str) -> Rich:
    """Convert WhatsApp-formatted text into plain text plus spans."""
    segments: List[Tuple[str, frozenset]] = []
    pos = 0
    for m in _CODE_BLOCK_RE.finditer(text):
        inner = m.group(1)
        if not inner.strip():
            continue
        _parse_lines(text[pos:m.start()], _at_line_start(text, pos), segments)
        segments.append((inner, frozenset({PRE if "\n" in inner else CODE})))
        pos = m.end()
    _parse_lines(text[pos:], _at_line_start(text, pos), segments)
    return _segments_to_rich(segments)


def _at_line_start(text: str, pos: int) -> bool:
    return pos == 0 or text[pos - 1] == "\n"


def _parse_lines(chunk: str, at_line_start: bool, segments: list) -> None:
    if not chunk:
        return
    lines = chunk.split("\n")
    quoted = [
        (at_line_start or i > 0) and line.startswith("> ")
        for i, line in enumerate(lines)
    ]
    for i, line in enumerate(lines):
        styles = frozenset({QUOTE}) if quoted[i] else frozenset()
        if quoted[i]:
            line = line[2:]
        _parse_inline_code(line, styles, segments)
        if i < len(lines) - 1:
            # The line break between two quoted lines belongs to the quote, so
            # consecutive "> " lines become one blockquote.
            keep = quoted[i] and quoted[i + 1]
            segments.append(("\n", frozenset({QUOTE}) if keep else frozenset()))


def _parse_inline_code(line: str, styles: frozenset, segments: list) -> None:
    pos = 0
    for m in _INLINE_CODE_RE.finditer(line):
        _parse_styles(line[pos:m.start()], styles, segments)
        segments.append((m.group(1), styles | {CODE}))
        pos = m.end()
    _parse_styles(line[pos:], styles, segments)


def _parse_styles(s: str, styles: frozenset, segments: list) -> None:
    if not s:
        return
    # Characters inside links can't open or close formatting.
    in_url = [False] * len(s)
    for m in _URL_RE.finditer(s):
        for i in range(m.start(), m.end()):
            in_url[i] = True
    _parse_range(s, 0, len(s), styles, in_url, segments)


def _parse_range(s: str, lo: int, hi: int, styles: frozenset, in_url: list, segments: list) -> None:
    i = lo
    text_start = lo
    while i < hi:
        kind = _MARKERS.get(s[i])
        if kind and not in_url[i] and kind not in styles and _can_open(s, i, hi):
            j = _find_close(s, i, hi, in_url)
            if j is not None:
                if text_start < i:
                    segments.append((s[text_start:i], styles))
                _parse_range(s, i + 1, j, styles | {kind}, in_url, segments)
                i = j + 1
                text_start = i
                continue
        i += 1
    if text_start < hi:
        segments.append((s[text_start:hi], styles))


def _can_open(s: str, i: int, hi: int) -> bool:
    # Opening marker: not glued to a word on its left (so snake_case and
    # 2*3*4 stay as they are), and followed by a non-space character.
    if i > 0 and s[i - 1].isalnum():
        return False
    if i + 1 >= hi:
        return False
    nxt = s[i + 1]
    return not nxt.isspace() and nxt != s[i]


def _find_close(s: str, i: int, hi: int, in_url: list) -> Optional[int]:
    marker = s[i]
    for j in range(i + 2, hi):
        if s[j] != marker or in_url[j]:
            continue
        if s[j - 1].isspace():
            continue
        if j + 1 < len(s) and s[j + 1].isalnum():
            continue
        return j
    return None


def _segments_to_rich(segments: List[Tuple[str, frozenset]]) -> Rich:
    text: List[str] = []
    spans: List[Span] = []
    open_at = {}
    pos = 0
    for seg, styles in segments:
        if not seg:
            continue
        for kind in [k for k in open_at if k not in styles]:
            spans.append(Span(kind, open_at.pop(kind), pos))
        for kind in styles:
            open_at.setdefault(kind, pos)
        text.append(seg)
        pos += len(seg)
    for kind, start in open_at.items():
        spans.append(Span(kind, start, pos))
    return Rich("".join(text), normalize(spans, pos))
