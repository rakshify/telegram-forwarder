"""Builders for WhatsApp message events, using neonize's own protobuf classes."""
from neonize.proto.Neonize_pb2 import Message as MessageEv
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage

GROUP = "120363000000000001@g.us"


def set_jid(target, s: str) -> None:
    user, _, server = s.partition("@")
    target.User = user
    target.Server = server
    target.RawAgent = 0
    target.Device = 0
    target.Integrator = 0


def event(msg: WAMessage = None, *, msg_id="WA1", chat=GROUP, sender="1111@lid",
          sender_alt="", recipient_alt="", pushname="Rahul", from_me=False, **flags) -> MessageEv:
    ev = MessageEv()
    info = ev.Info
    info.ID = msg_id
    info.Pushname = pushname
    info.Timestamp = 1700000000
    src = info.MessageSource
    set_jid(src.Chat, chat)
    set_jid(src.Sender, sender)
    if sender_alt:
        set_jid(src.SenderAlt, sender_alt)
    if recipient_alt:
        set_jid(src.RecipientAlt, recipient_alt)
    src.IsGroup = chat.endswith("@g.us")
    src.IsFromMe = from_me
    for name, value in flags.items():
        setattr(ev, name, value)
    if msg is not None:
        ev.Message.CopyFrom(msg)
    return ev


def text(s: str) -> WAMessage:
    return WAMessage(conversation=s)


def ext_text(s: str, *, reply_to: str = "", quoted_sender: str = "", quoted: WAMessage = None,
             mentions=()) -> WAMessage:
    m = WAMessage()
    m.extendedTextMessage.text = s
    ctx = m.extendedTextMessage.contextInfo
    if reply_to:
        ctx.stanzaID = reply_to
        ctx.participant = quoted_sender
        if quoted is not None:
            ctx.quotedMessage.CopyFrom(quoted)
    ctx.mentionedJID.extend(mentions)
    return m


def image(caption: str = "", w: int = 800, h: int = 600) -> WAMessage:
    m = WAMessage()
    m.imageMessage.caption = caption
    m.imageMessage.mimetype = "image/jpeg"
    m.imageMessage.width = w
    m.imageMessage.height = h
    return m


def video(caption: str = "", gif: bool = False, seconds: int = 12) -> WAMessage:
    m = WAMessage()
    v = m.videoMessage
    v.caption = caption
    v.mimetype = "video/mp4"
    v.gifPlayback = gif
    v.seconds = seconds
    v.width, v.height = 640, 360
    return m


def voice(seconds: int = 7, ptt: bool = True) -> WAMessage:
    m = WAMessage()
    a = m.audioMessage
    a.PTT = ptt
    a.seconds = seconds
    a.mimetype = "audio/ogg; codecs=opus" if ptt else "audio/mpeg"
    a.waveform = bytes([0, 50, 100] * 21 + [10])
    return m


def document(name: str = "report.pdf", caption: str = "") -> WAMessage:
    m = WAMessage()
    d = m.documentMessage
    d.fileName = name
    d.caption = caption
    d.mimetype = "application/pdf"
    return m


def sticker(animated: bool = False, lottie: bool = False) -> WAMessage:
    m = WAMessage()
    s = m.stickerMessage
    s.mimetype = "image/webp"
    s.isAnimated = animated
    s.isLottie = lottie
    return m


def poll(question: str, options, selectable: int = 1, field: str = "pollCreationMessageV3") -> WAMessage:
    m = WAMessage()
    p = getattr(m, field)
    p.name = question
    for o in options:
        p.options.add().optionName = o
    p.selectableOptionsCount = selectable
    return m


def edit(target_id: str, new_text: str) -> WAMessage:
    m = WAMessage()
    pm = m.protocolMessage
    pm.type = 14  # MESSAGE_EDIT
    pm.key.ID = target_id
    pm.editedMessage.conversation = new_text
    return m


def wrap(field: str, inner: WAMessage) -> WAMessage:
    m = WAMessage()
    getattr(m, field).message.CopyFrom(inner)
    return m
