"""WhatsApp message event -> a plan of what to post, on any destination.

Pure functions over the event's protobuf objects. Nothing here talks to
the network or imports neonize (protobuf objects are read by attribute
only), so this is unit-testable and the neonize surface stays in client.py.

`plan_message(event, names)` returns either a `Plan` (something to post or
an edit to apply) or a `Skip` with the reason. Sender attribution, reply
threading and quote fallbacks depend on the pair and on what was already
forwarded, so the bridge applies those; the plan only carries the facts.
"""
import datetime
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple, Union

from ...core import model
from ...core import rich as fmt
from ...core.model import Poll, Skip
from .formatting import parse_whatsapp

PROTOCOL_REVOKE = 0
PROTOCOL_MESSAGE_EDIT = 14

# Envelopes whose payload is a nested Message in `.message`. whatsmeow
# unwraps the common ones already; the newer ones may still arrive wrapped.
_WRAPPERS = (
    "deviceSentMessage", "ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2",
    "viewOnceMessageV2Extension", "documentWithCaptionMessage", "editedMessage",
    "groupMentionedMessage", "associatedChildMessage", "spoilerMessage",
    "botForwardedMessage", "botInvokeMessage", "questionMessage",
    "lottieStickerMessage", "audioStickerMessage", "pollCreationMessageV4",
    "pollCreationMessageV5", "pollCreationMessageV6",
)
_VIEW_ONCE = {"viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension"}

# Fields that ride along with real messages, or bookkeeping messages that
# carry nothing to forward. A message made only of these is skipped quietly.
_QUIET = {
    "senderKeyDistributionMessage", "fastRatchetKeySenderKeyDistributionMessage",
    "messageContextInfo", "reactionMessage", "encReactionMessage", "pollUpdateMessage",
    "keepInChatMessage", "pinInChatMessage", "albumMessage", "encCommentMessage",
    "encEventResponseMessage", "call", "chat", "placeholderMessage",
    "secretEncryptedMessage", "messageHistoryBundle", "messageHistoryNotice",
    "stickerSyncRmrMessage", "callLogMesssage", "bcallMessage", "groupRootKeyShare",
    "rootSecretDistributeMessage", "statusNotificationMessage",
}

_POLL_FIELDS = ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3")
_TEXTISH_FIELDS = (
    "buttonsMessage", "templateMessage", "listMessage", "interactiveMessage",
    "highlyStructuredMessage", "productMessage", "orderMessage", "invoiceMessage",
    "musicMessage", "requestPhoneNumberMessage",
)

QUOTE_PREVIEW_LIMIT = 200


@dataclass
class Media(model.Media):
    """A WhatsApp attachment: the neutral description, plus what downloading it takes."""
    field: str = ""        # Message field that holds it when downloading
    proto: object = None   # the media sub-message (ImageMessage, VideoMessage, ...)


@dataclass
class Quote:
    msg_id: str           # WhatsApp id of the message being replied to
    sender: str           # its sender's id ('' if unknown)
    preview: fmt.Rich     # short preview, for when it was never forwarded


@dataclass
class Plan:
    kind: str                         # text | media | poll | edit
    label: str                        # short description for logs
    body: fmt.Rich = field(default_factory=fmt.Rich)  # text or caption
    media: Optional[Media] = None
    poll: Optional[Poll] = None
    reply: Optional[Quote] = None
    edit_of: str = ""                 # kind == "edit": id of the edited message


Names = Callable[[str], Optional[str]]


# ------------------------------------------------------------------ helpers

def _has(msg, name: str) -> bool:
    try:
        return msg.HasField(name)
    except ValueError:  # field not in this proto version
        return False


def set_fields(msg) -> List[str]:
    return [fd.name for fd, _ in msg.ListFields()]


def unwrap(msg) -> Tuple[object, set]:
    """Peel envelopes off a Message; returns (inner message, envelope names)."""
    seen = set()
    for _ in range(8):
        for name in _WRAPPERS:
            if _has(msg, name) and _has(getattr(msg, name), "message"):
                seen.add(name)
                msg = getattr(msg, name).message
                break
        else:
            break
    return msg, seen


def _ctx(sub):
    if sub is not None and not isinstance(sub, str) and _has(sub, "contextInfo"):
        return sub.contextInfo
    return None


def _render(text: str, ctx, names: Names, spoiler: bool = False) -> fmt.Rich:
    rich = parse_whatsapp(text or "")
    if ctx is not None and len(ctx.mentionedJID):
        rich = resolve_mentions(rich, list(ctx.mentionedJID), names)
    rich = fmt.strip(rich)
    if spoiler and rich.text:
        rich = fmt.Rich(rich.text, rich.spans + [fmt.Span(fmt.SPOILER, 0, len(rich.text))])
    return rich


def resolve_mentions(rich: fmt.Rich, jids: Sequence[str], names: Names) -> fmt.Rich:
    """WhatsApp writes mentions as @<number or id>; show the person's name
    instead. Unknown people become @someone, so phone numbers never leak."""
    reps = []
    for jid in jids:
        user = jid.split("@", 1)[0].split(":", 1)[0]
        if not user:
            continue
        shown = "@" + (names(jid) or "someone")
        for m in re.finditer(re.escape("@" + user) + r"(?!\d)", rich.text):
            reps.append((m.start(), m.end(), shown))
    reps.sort()
    kept, last_end = [], -1
    for r in reps:
        if r[0] >= last_end:
            kept.append(r)
            last_end = r[1]
    return fmt.replace_ranges(rich, kept) if kept else rich


def _harvest_text(sub, depth: int = 4) -> List[str]:
    """Best-effort text from business/template/interactive messages."""
    wanted = {"text", "contentText", "hydratedContentText", "description", "title",
              "caption", "footerText", "hydratedFooterText", "name", "body"}
    out: List[str] = []
    if depth <= 0 or sub is None:
        return out
    for fd, value in sub.ListFields():
        items = list(value) if _is_repeated(fd) else [value]
        if fd.type == fd.TYPE_STRING:
            if fd.name in wanted:
                out.extend(v.strip() for v in items if v.strip())
        elif fd.type == fd.TYPE_MESSAGE:
            for item in items:
                out.extend(_harvest_text(item, depth - 1))
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _is_repeated(fd) -> bool:
    flag = getattr(fd, "is_repeated", None)  # newer protobuf
    if flag is not None:
        return bool(flag)
    return fd.label == fd.LABEL_REPEATED


def _vcard_phones(vcard: str) -> List[str]:
    phones = []
    for line in (vcard or "").splitlines():
        if line.upper().startswith(("TEL", "ITEM1.TEL", "ITEM2.TEL")) and ":" in line:
            value = line.rsplit(":", 1)[1].strip()
            if value:
                phones.append(value)
    return phones


def _maps_link(lat: float, lng: float) -> str:
    return f"https://maps.google.com/?q={lat:.6f},{lng:.6f}"


# ------------------------------------------------------------------ classify

def classify(msg) -> Tuple[str, str, object]:
    """(kind, field name, sub-message) of a Message's main content.

    kind is one of: poll, photo, video, animation, voice, audio, document,
    sticker, text, location, contact, event, invite, textish, quiet, unknown.
    """
    fields = set_fields(msg)
    for name in _POLL_FIELDS:
        if name in fields:
            return "poll", name, getattr(msg, name)
    for name in ("imageMessage", "instantImageMessage"):
        if name in fields:
            return "photo", name, getattr(msg, name)
    for name in ("videoMessage", "instantVideoMessage", "ptvMessage"):
        if name in fields:
            sub = getattr(msg, name)
            return ("animation" if sub.gifPlayback else "video"), name, sub
    if "audioMessage" in fields:
        sub = msg.audioMessage
        return ("voice" if sub.PTT else "audio"), "audioMessage", sub
    if "documentMessage" in fields:
        return "document", "documentMessage", msg.documentMessage
    if "stickerMessage" in fields:
        return "sticker", "stickerMessage", msg.stickerMessage
    if "extendedTextMessage" in fields:
        return "text", "extendedTextMessage", msg.extendedTextMessage
    if "conversation" in fields:
        return "text", "conversation", msg.conversation
    for name in ("locationMessage", "liveLocationMessage"):
        if name in fields:
            return "location", name, getattr(msg, name)
    for name in ("contactMessage", "contactsArrayMessage"):
        if name in fields:
            return "contact", name, getattr(msg, name)
    if "eventMessage" in fields:
        return "event", "eventMessage", msg.eventMessage
    if "groupInviteMessage" in fields:
        return "invite", "groupInviteMessage", msg.groupInviteMessage
    for name in _TEXTISH_FIELDS:
        if name in fields:
            return "textish", name, getattr(msg, name)
    if not fields or set(fields) <= _QUIET:
        return "quiet", ",".join(fields), None
    return "unknown", ",".join(f for f in fields if f not in _QUIET), None


_LABELS = {
    "photo": "📷 Photo", "video": "🎥 Video", "animation": "GIF",
    "voice": "🎤 Voice message", "audio": "🎵 Audio", "document": "📄 Document",
    "sticker": "Sticker", "poll": "📊 Poll", "location": "📍 Location",
    "contact": "👤 Contact", "event": "📅 Event", "invite": "🔗 Group invite",
}


def _own_text(kind: str, sub) -> str:
    """The text or caption the sender typed (empty if none)."""
    if kind == "text":
        return sub if isinstance(sub, str) else sub.text
    if kind in ("photo", "video", "animation", "document"):
        return sub.caption
    if kind == "textish":
        return "\n".join(_harvest_text(sub))
    return ""


def _text_of(kind: str, field_name: str, sub) -> str:
    """Text for a quote preview: the message's text, or a label like 📷 Photo."""
    own = _own_text(kind, sub)
    if own:
        return own
    if kind == "document":
        return f"📄 {sub.fileName or sub.title or 'Document'}"
    if kind == "poll":
        return f"📊 {sub.name}"
    return _LABELS.get(kind, "")


def preview(msg, names: Names) -> fmt.Rich:
    """Short formatted preview of a (quoted) message."""
    if msg is None:
        return fmt.Rich()
    msg, _ = unwrap(msg)
    kind, field_name, sub = classify(msg)
    text = _text_of(kind, field_name, sub)
    rich = _render(text, _ctx(sub), names)
    return fmt.truncate(rich, QUOTE_PREVIEW_LIMIT)


# ------------------------------------------------------------------ plan

def is_bookkeeping(ev) -> bool:
    """True for events with nothing to forward.

    WhatsApp often delivers one message as two events with the same id: an
    encryption-key handover (senderKeyDistributionMessage) first, then the
    content. The first kind must not take the message's place in the queue.
    """
    msg, _ = unwrap(ev.Message)
    if _has(msg, "protocolMessage"):
        return msg.protocolMessage.type not in (PROTOCOL_MESSAGE_EDIT, PROTOCOL_REVOKE)
    kind, _, _ = classify(msg)
    return kind == "quiet"


def plan_message(ev, names: Names) -> Union[Plan, Skip]:
    msg, envelopes = unwrap(ev.Message)
    if envelopes & _VIEW_ONCE or ev.IsViewOnce:
        return Skip("view-once media (WhatsApp doesn't give it to linked devices)")
    spoiler = "spoilerMessage" in envelopes

    if _has(msg, "protocolMessage"):
        pm = msg.protocolMessage
        if pm.type == PROTOCOL_MESSAGE_EDIT and pm.key.ID:
            new_msg, _ = unwrap(pm.editedMessage)
            kind, field_name, sub = classify(new_msg)
            if kind in ("quiet", "unknown"):
                return Skip("edit with no text", quiet=True)
            body = _render(_own_text(kind, sub), _ctx(sub), names, spoiler)
            return Plan(kind="edit", label="edit", body=body, edit_of=pm.key.ID)
        if pm.type == PROTOCOL_REVOKE:
            return Skip("deleted for everyone (deletions aren't mirrored)")
        return Skip("protocol message", quiet=True)

    kind, field_name, sub = classify(msg)
    if kind == "quiet":
        return Skip(f"no content ({field_name or 'empty'})", quiet=True)
    if kind == "unknown":
        return Skip(f"unsupported message type ({field_name})")

    ctx = _ctx(sub)
    reply = None
    if ctx is not None and ctx.stanzaID:
        reply = Quote(
            msg_id=ctx.stanzaID,
            sender=ctx.participant,
            preview=preview(ctx.quotedMessage if _has(ctx, "quotedMessage") else None, names),
        )

    def plan(kind_, label, text, **kw) -> Plan:
        return Plan(kind=kind_, label=label, body=_render(text, ctx, names, spoiler),
                    reply=reply, **kw)

    if kind == "text":
        return plan("text", "text", _own_text(kind, sub))

    if kind == "poll":
        options = [o.optionName for o in sub.options if o.optionName]
        return Plan(
            kind="poll", label="poll", reply=reply,
            poll=Poll(question=sub.name, options=options,
                      multiple=sub.selectableOptionsCount != 1),
        )

    if kind == "sticker":
        if sub.isLottie or ev.IsLottieSticker:
            return Skip("animated (Lottie) sticker, which Telegram can't show")
        media = Media(kind="sticker", field="stickerMessage", proto=sub,
                      mimetype=sub.mimetype or "image/webp", animated=sub.isAnimated,
                      width=sub.width, height=sub.height)
        return Plan(kind="media", label="sticker", media=media, reply=reply)

    if kind in ("photo", "video", "animation", "voice", "audio", "document"):
        canonical = {
            "photo": "imageMessage", "video": "videoMessage", "animation": "videoMessage",
            "voice": "audioMessage", "audio": "audioMessage", "document": "documentMessage",
        }[kind]
        media = Media(kind=kind, field=canonical, proto=sub, mimetype=sub.mimetype)
        if kind in ("photo", "video", "animation"):
            media.width, media.height = sub.width, sub.height
        if kind in ("video", "animation", "voice", "audio"):
            media.seconds = sub.seconds
        if kind == "voice":
            media.waveform = bytes(sub.waveform)
        if kind == "document":
            media.filename = sub.fileName or sub.title or ""
        caption = "" if kind in ("voice", "audio") else sub.caption
        return plan("media", kind, caption, media=media)

    if kind == "location":
        lat, lng = sub.degreesLatitude, sub.degreesLongitude
        if field_name == "liveLocationMessage":
            lines = ["📍 Live location (shared on WhatsApp)", sub.caption]
        else:
            lines = [f"📍 {sub.name}" if sub.name else "📍 Location", sub.address, sub.comment]
        lines.append(_maps_link(lat, lng))
        return plan("text", "location", "\n".join(l for l in lines if l))

    if kind == "contact":
        cards = [sub] if field_name == "contactMessage" else list(sub.contacts)
        lines = []
        for card in cards:
            lines.append(f"👤 {card.displayName or 'Contact'}")
            lines.extend(_vcard_phones(card.vcard))
        return plan("text", "contact", "\n".join(lines))

    if kind == "event":
        lines = [("❌ Cancelled: " if sub.isCanceled else "📅 ") + (sub.name or "Event")]
        if sub.startTime:
            start = datetime.datetime.fromtimestamp(sub.startTime, datetime.timezone.utc)
            lines.append(start.strftime("🕒 %Y-%m-%d %H:%M UTC"))
        if _has(sub, "location") and (sub.location.name or sub.location.degreesLatitude):
            loc = sub.location
            lines.append(f"📍 {loc.name}" if loc.name else _maps_link(loc.degreesLatitude, loc.degreesLongitude))
        if sub.description:
            lines.append(sub.description)
        return plan("text", "event", "\n".join(lines))

    if kind == "invite":
        lines = [f"🔗 Invite to the WhatsApp group “{sub.groupName}”" if sub.groupName
                 else "🔗 WhatsApp group invite"]
        if sub.caption:
            lines.append(sub.caption)
        return plan("text", "invite", "\n".join(lines))

    # textish: business, template, list and button messages
    texts = _harvest_text(sub)
    if not texts:
        return Skip(f"{field_name} without text")
    return plan("text", field_name, "\n".join(texts))
