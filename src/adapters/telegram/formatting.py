"""Telegram message entities <-> Rich text (src/core/rich.py).

Telegram sends plain text plus "entities": typed ranges with UTF-16 offsets.
Every entity type maps to a Rich span, with what it carries (a link's URL, a
mention's user id, a custom emoji's id, a code block's language) in
Span.data, so Telegram -> Telegram comes out exactly as it went in. Types
this module doesn't know (newer layers) travel as RAW spans holding the
entity's own TL bytes, and come back unchanged.
"""
import base64
from typing import List, Optional, Tuple

from telethon.extensions import BinaryReader
from telethon.tl import types

from ...core import rich as fmt

_RAW_PREFIX = "telegram:"

_STYLES = {
    types.MessageEntityBold: fmt.BOLD,
    types.MessageEntityItalic: fmt.ITALIC,
    types.MessageEntityUnderline: fmt.UNDERLINE,
    types.MessageEntityStrike: fmt.STRIKE,
    types.MessageEntitySpoiler: fmt.SPOILER,
    types.MessageEntityCode: fmt.CODE,
    types.MessageEntityUrl: fmt.URL,
    types.MessageEntityEmail: fmt.EMAIL,
    types.MessageEntityPhone: fmt.PHONE,
    types.MessageEntityMention: fmt.MENTION,
    types.MessageEntityHashtag: fmt.HASHTAG,
    types.MessageEntityCashtag: fmt.CASHTAG,
    types.MessageEntityBotCommand: fmt.BOT_COMMAND,
    types.MessageEntityBankCard: fmt.BANK_CARD,
}
_MAKERS = {kind: cls for cls, kind in _STYLES.items()}


def _kind_of(e) -> Optional[Tuple[str, str]]:
    """(span kind, data) for a Telethon entity; None to drop it."""
    cls = type(e)
    if cls in _STYLES:
        return _STYLES[cls], ""
    if cls is types.MessageEntityPre:
        return fmt.PRE, e.language or ""
    if cls is types.MessageEntityBlockquote:
        return fmt.QUOTE, "collapsed" if e.collapsed else ""
    if cls is types.MessageEntityTextUrl:
        return fmt.TEXT_URL, e.url
    if cls is types.MessageEntityMentionName:
        return fmt.MENTION_NAME, str(e.user_id)
    if cls is types.MessageEntityCustomEmoji:
        return fmt.CUSTOM_EMOJI, str(e.document_id)
    if cls in (types.MessageEntityUnknown, types.InputMessageEntityMentionName):
        return None
    return fmt.RAW, _RAW_PREFIX + base64.b64encode(bytes(e)).decode("ascii")


def to_rich(text: Optional[str], entities) -> fmt.Rich:
    """A Telegram message's text and entities -> Rich."""
    text = text or ""
    if not entities:
        return fmt.Rich(text, [])
    offsets = fmt.utf16_offsets(text)
    spans: List[fmt.Span] = []
    for e in entities:
        kind_data = _kind_of(e)
        if kind_data is None:
            continue
        start = fmt.from_utf16(offsets, e.offset)
        end = fmt.from_utf16(offsets, e.offset + e.length)
        if end > start:
            spans.append(fmt.Span(kind_data[0], start, end, kind_data[1]))
    return fmt.Rich(text, fmt.normalize(spans, len(text)))


def to_entities(rich: fmt.Rich) -> list:
    """Rich -> Telethon MessageEntity objects with UTF-16 offsets."""
    offsets = fmt.utf16_offsets(rich.text)
    entities = []
    for s in rich.spans:
        offset = offsets[s.start]
        length = offsets[s.end] - offset
        if length <= 0:
            continue
        e = _make(s, offset, length)
        if e is not None:
            entities.append(e)
    return entities


def _make(s: fmt.Span, offset: int, length: int):
    if s.kind in _MAKERS:
        return _MAKERS[s.kind](offset=offset, length=length)
    if s.kind == fmt.PRE:
        return types.MessageEntityPre(offset=offset, length=length, language=s.data)
    if s.kind == fmt.QUOTE:
        return types.MessageEntityBlockquote(offset=offset, length=length,
                                             collapsed=True if s.data == "collapsed" else None)
    if s.kind == fmt.TEXT_URL:
        return types.MessageEntityTextUrl(offset=offset, length=length, url=s.data)
    if s.kind == fmt.MENTION_NAME:
        return types.MessageEntityMentionName(offset=offset, length=length, user_id=int(s.data))
    if s.kind == fmt.CUSTOM_EMOJI:
        return types.MessageEntityCustomEmoji(offset=offset, length=length, document_id=int(s.data))
    if s.kind == fmt.RAW and s.data.startswith(_RAW_PREFIX):
        e = BinaryReader(base64.b64decode(s.data[len(_RAW_PREFIX):])).tgread_object()
        e.offset, e.length = offset, length
        return e
    return None  # another platform's span: nothing to show for it here
