"""Telegram message -> a neutral Message, as the pipeline posts it.

Pure functions over Telethon's message objects (as read back from the
queue); nothing here talks to the network. Media kinds are decided in the
order the old forwarder used (voice, round video, photo, GIF, video, audio,
sticker, document), and keep the document's own attributes (duration, size,
waveform, title) so a sink can re-create them faithfully.
"""
from dataclasses import dataclass
from typing import Optional, Union

from telethon.tl import types

from ...core import model
from ...core import rich as fmt
from ...core.model import Message, Poll, Quote, Skip
from .formatting import to_rich


@dataclass
class TgMedia(model.Media):
    """A Telegram attachment: the neutral description, plus what downloading it takes."""
    ref: object = None  # the message's media object (what download_media takes)
    chat_id: int = 0    # where the message is, to fetch it again if its file reference expired
    msg_id: int = 0


def sender_display(sender) -> str:
    """Render a sender as 'First Last (@username)' / 'First (@username)' /
    'First Last' / 'First' / '(unknown user)' depending on what's available.
    """
    if sender is None:
        return "(unknown user)"
    if not isinstance(sender, types.User):
        # Channel posts, anonymous admin posts, etc. — use the chat title.
        title = getattr(sender, "title", None)
        return title or "(unknown sender)"

    parts = [p for p in (sender.first_name, sender.last_name) if p]
    name = " ".join(parts) if parts else None

    if name and sender.username:
        return f"{name} (@{sender.username})"
    if name:
        return name
    if sender.username:
        return f"@{sender.username}"
    return "(unknown user)"


def chat_id_of(peer) -> int:
    """Bare id of a Peer (PeerChannel, PeerChat or PeerUser)."""
    for attr in ("channel_id", "chat_id", "user_id"):
        value = getattr(peer, attr, None)
        if value:
            return value
    return 0


def topic_of(msg) -> int:
    """The forum topic a message belongs to, 0 if none: reply_to_top_id for a
    reply inside a topic, else reply_to_msg_id (the topic's root message, or
    a direct reply to it). Messages in the General topic or in chats without
    topics have no reply header at all."""
    rt = getattr(msg, "reply_to", None)
    if rt is None:
        return 0
    return getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None) or 0


def to_message(msg, sender_name: str) -> Union[Message, Skip]:
    if isinstance(msg, types.MessageService):
        return Skip("service message (join, pin, ...)", quiet=True)
    if not isinstance(msg, types.Message):
        return Skip(f"not a message ({type(msg).__name__})", quiet=True)

    body = to_rich(msg.message, msg.entities)
    common = dict(id=str(msg.id), sender_name=sender_name, reply=_reply(msg))
    media = msg.media
    if isinstance(media, types.MessageMediaPoll):
        return Message(kind="poll", label="poll", poll=_poll(media), **common)
    if isinstance(media, (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue)):
        return Message(kind="text", label="location", body=_location(media, body), **common)

    # A link preview is rebuilt by Telegram from the text, so it counts as text.
    plain = media is None or isinstance(media, types.MessageMediaWebPage)
    spec = None if plain else _media(msg)
    if spec is not None:
        return Message(kind="media", label=spec.kind, body=body, media=spec, **common)
    if body.text:
        return Message(kind="text", label="text", body=body, **common)
    if plain:
        return Skip("empty message", quiet=True)
    return Skip(f"nothing to forward ({type(media).__name__})")


def _reply(msg) -> Optional[Quote]:
    rt = getattr(msg, "reply_to", None)
    if rt is None or getattr(rt, "reply_to_peer_id", None) is not None:
        return None  # not a reply, or a reply to a message in another chat
    reply_id = getattr(rt, "reply_to_msg_id", None)
    return Quote(str(reply_id)) if reply_id else None


def _attr(document, cls):
    for a in getattr(document, "attributes", None) or []:
        if isinstance(a, cls):
            return a
    return None


def _waveform(packed) -> bytes:
    """Telegram's packed 5-bit samples -> amplitudes of 0-100 (the neutral form).

    Unpacked here rather than with telethon.utils.decode_waveform, which
    reads the last sample from the wrong bits."""
    if not packed:
        return b""
    bits = int.from_bytes(packed, "little")
    samples = [(bits >> (5 * i)) & 31 for i in range(len(packed) * 8 // 5)]
    return bytes(min(100, round(v * 100 / 31)) for v in samples)


def _media(msg) -> Optional[TgMedia]:
    media = msg.media
    ref = dict(ref=media, chat_id=chat_id_of(msg.peer_id), msg_id=msg.id)
    if isinstance(media, types.MessageMediaContact):
        name = " ".join(p for p in (media.first_name, media.last_name) if p) or "contact"
        return TgMedia(kind="document", mimetype="text/vcard", filename=f"{name}.vcf", **ref)

    doc = msg.document
    mime = getattr(doc, "mime_type", "") or ""
    video = _attr(doc, types.DocumentAttributeVideo)
    audio = _attr(doc, types.DocumentAttributeAudio)
    named = _attr(doc, types.DocumentAttributeFilename)
    filename = named.file_name if named else ""
    seconds = int(round(getattr(video or audio, "duration", 0) or 0))
    size = dict(width=getattr(video, "w", 0) or 0, height=getattr(video, "h", 0) or 0)

    if msg.voice:
        return TgMedia(kind="voice", mimetype=mime, seconds=seconds,
                       waveform=_waveform(getattr(audio, "waveform", None)), **ref)
    if msg.video_note:
        return TgMedia(kind="video_note", mimetype=mime, seconds=seconds, **size, **ref)
    if msg.photo:
        return TgMedia(kind="photo", mimetype="image/jpeg", **ref)
    if msg.gif:
        return TgMedia(kind="animation", mimetype=mime, seconds=seconds, **size, **ref)
    if msg.video:
        return TgMedia(kind="video", mimetype=mime, seconds=seconds, **size, **ref)
    if msg.audio:
        return TgMedia(kind="audio", mimetype=mime, seconds=seconds,
                       title=getattr(audio, "title", None) or "",
                       performer=getattr(audio, "performer", None) or "", **ref)
    if msg.sticker:
        if mime == "image/webp":
            return TgMedia(kind="sticker", mimetype=mime, **ref)
        # Animated (.tgs) stickers can't be re-made as stickers by a bot: send the file.
        return TgMedia(kind="document", mimetype=mime, filename=filename, **ref)
    if doc is not None:
        return TgMedia(kind="document", mimetype=mime, filename=filename, **ref)
    return None


def _text(value) -> str:
    return value.text if isinstance(value, types.TextWithEntities) else (value or "")


def _poll(media) -> Poll:
    poll = media.poll
    answers = list(poll.answers)
    correct = None
    if poll.quiz and media.results is not None:
        right = {r.option for r in (media.results.results or []) if r.correct}
        correct = next((i for i, a in enumerate(answers) if a.option in right), None)
    return Poll(
        question=_text(poll.question),
        options=[_text(a.text) for a in answers],
        multiple=bool(poll.multiple_choice),
        quiz=bool(poll.quiz),
        correct=correct,
        public=bool(poll.public_voters),
        close_period=poll.close_period or 0,
        close_date=int(poll.close_date.timestamp()) if poll.close_date else None,
    )


def _location(media, body: fmt.Rich) -> fmt.Rich:
    geo = media.geo
    if isinstance(media, types.MessageMediaVenue):
        lines = [f"📍 {media.title}" if media.title else "📍 Location", media.address]
    elif isinstance(media, types.MessageMediaGeoLive):
        lines = ["📍 Live location (shared on Telegram)"]
    else:
        lines = ["📍 Location"]
    if isinstance(geo, types.GeoPoint):
        lines.append(f"https://maps.google.com/?q={geo.lat:.6f},{geo.long:.6f}")
    text = fmt.plain("\n".join(line for line in lines if line))
    return fmt.concat(text, fmt.plain("\n\n"), body) if body.text else text
