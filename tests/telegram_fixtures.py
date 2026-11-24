"""Telegram messages built from Telethon's own TL classes (no network), and a fake user client."""
import asyncio
import datetime
import os

from telethon import errors
from telethon import utils as tl_utils
from telethon.tl import types

CHAT = 1001  # bare id of the source supergroup
DATE = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
WAVEFORM = tl_utils.encode_waveform(bytes([0, 31, 15, 7] * 25))  # 100 samples, as Telegram packs them


def user(first="Alex", last="Doe", username="alex_doe", uid=7):
    return types.User(id=uid, first_name=first, last_name=last, username=username)


def channel(title="Market News", cid=CHAT):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=DATE)


def reply_header(reply_to=0, topic=0):
    if not reply_to and not topic:
        return None
    if topic and not reply_to:  # a plain message inside a topic points at the topic's root
        return types.MessageReplyHeader(reply_to_msg_id=topic, forum_topic=True)
    return types.MessageReplyHeader(reply_to_msg_id=reply_to, reply_to_top_id=topic or None,
                                    forum_topic=True if topic else None)


def message(id=1, text="", entities=None, media=None, reply_to=0, topic=0, chat=CHAT, sender=None):
    m = types.Message(id=id, peer_id=types.PeerChannel(chat), date=DATE, message=text,
                      entities=entities, media=media, from_id=types.PeerUser(7),
                      reply_to=reply_header(reply_to, topic))
    m._sender = sender if sender is not None else user()  # what Telethon fills in from the update
    return m


def service(id=1, chat=CHAT):
    m = types.MessageService(id=id, peer_id=types.PeerChannel(chat), date=DATE,
                             action=types.MessageActionPinMessage())
    m._sender = user()
    return m


def _document(mime, attributes, id=500):
    return types.Document(id=id, access_hash=1, file_reference=b"ref", date=DATE, mime_type=mime,
                          size=1234, dc_id=4, attributes=attributes)


def photo():
    return types.MessageMediaPhoto(photo=types.Photo(
        id=600, access_hash=1, file_reference=b"ref", date=DATE, dc_id=4,
        sizes=[types.PhotoSize(type="x", w=800, h=600, size=1000)]))


def voice():
    return types.MessageMediaDocument(voice=True, document=_document("audio/ogg", [
        types.DocumentAttributeAudio(duration=9, voice=True, waveform=WAVEFORM)]))


def video_note():
    return types.MessageMediaDocument(round=True, document=_document("video/mp4", [
        types.DocumentAttributeVideo(duration=5.0, w=384, h=384, round_message=True)]))


def gif():
    return types.MessageMediaDocument(document=_document("video/mp4", [
        types.DocumentAttributeVideo(duration=2.0, w=320, h=240), types.DocumentAttributeAnimated(),
        types.DocumentAttributeFilename(file_name="anim.mp4")]))


def video():
    return types.MessageMediaDocument(video=True, document=_document("video/mp4", [
        types.DocumentAttributeVideo(duration=12.4, w=1280, h=720, supports_streaming=True),
        types.DocumentAttributeFilename(file_name="clip.mp4")]))


def audio():
    return types.MessageMediaDocument(document=_document("audio/mpeg", [
        types.DocumentAttributeAudio(duration=180, title="Song", performer="Band"),
        types.DocumentAttributeFilename(file_name="song.mp3")]))


def sticker():
    return types.MessageMediaDocument(document=_document("image/webp", [
        types.DocumentAttributeImageSize(w=512, h=512),
        types.DocumentAttributeSticker(alt="😀", stickerset=types.InputStickerSetEmpty()),
        types.DocumentAttributeFilename(file_name="sticker.webp")]))


def document(name="Q3 results.pdf", mime="application/pdf"):
    return types.MessageMediaDocument(document=_document(mime, [
        types.DocumentAttributeFilename(file_name=name)]))


def poll(question="Which?", options=("TCS", "INFY"), multiple=True, public=True, quiz=False,
         correct=None, close_period=None):
    answers = [types.PollAnswer(text=types.TextWithEntities(text=o, entities=[]), option=bytes([48 + i]))
               for i, o in enumerate(options)]
    results = types.PollResults(results=[
        types.PollAnswerVoters(option=a.option, correct=True if i == correct else None)
        for i, a in enumerate(answers)] if correct is not None else None)
    return types.MessageMediaPoll(poll=types.Poll(
        id=1, question=types.TextWithEntities(text=question, entities=[]), answers=answers, hash=0,
        multiple_choice=multiple or None, public_voters=public or None, quiz=quiz or None,
        close_period=close_period), results=results)


def webpage():
    return types.MessageMediaWebPage(webpage=types.WebPageEmpty(id=1))


def geo(lat=12.9716, long=77.5946):
    return types.MessageMediaGeo(geo=types.GeoPoint(long=long, lat=lat, access_hash=0))


def contact():
    return types.MessageMediaContact(phone_number="+15550000001", first_name="Bob", last_name="Lee",
                                     vcard="", user_id=0)


def downloaded_name(media) -> str:
    """The file name Telethon gives a download (by the document's name, or by type)."""
    if isinstance(media, types.MessageMediaPhoto):
        return "photo.jpg"
    if isinstance(media, types.MessageMediaContact):
        return "contact.vcard"
    doc = media.document
    for a in doc.attributes:
        if isinstance(a, types.DocumentAttributeFilename):
            return a.file_name
    return {"audio/ogg": "voice.oga", "video/mp4": "video.mp4"}.get(doc.mime_type, "file")


class _Disconnected:
    def __init__(self):
        self.callbacks = []

    def add_done_callback(self, cb):
        self.callbacks.append(cb)


class FakeUserClient:
    """The parts of a Telethon user client the Telegram source (and the old forwarder) use."""

    def __init__(self, history=(), authorized=True):
        self.history = list(history)  # messages of the source chat, any order
        self.authorized = authorized
        self.connected = False
        self.handlers = []
        self.downloads = []
        self.expire_next_download = False
        self.disconnected = _Disconnected()

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return self.authorized

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    async def iter_dialogs(self):
        for _ in ():
            yield _

    def _in_scope(self, reply_to):
        from src.adapters.telegram.convert import topic_of
        return [m for m in self.history if not reply_to or topic_of(m) == reply_to]

    async def get_messages(self, peer, limit=None, ids=None, reply_to=None):
        if ids is not None:
            return next((m for m in self.history if m.id == ids), None)
        newest_first = sorted(self._in_scope(reply_to), key=lambda m: -m.id)
        return newest_first[:limit] if limit else newest_first

    async def iter_messages(self, peer, min_id=0, reply_to=None):
        for m in sorted(self._in_scope(reply_to), key=lambda m: -m.id):  # newest first, like Telegram
            if m.id > min_id:
                yield m

    async def download_media(self, media, file=None):
        media = getattr(media, "media", media)  # the old forwarder passes the message
        if not isinstance(media, (types.MessageMediaPhoto, types.MessageMediaDocument,
                                  types.MessageMediaContact)):
            return None  # like Telethon: nothing to download for locations, dice, ...
        self.downloads.append(media)
        if self.expire_next_download:
            self.expire_next_download = False
            raise errors.FileReferenceExpiredError(request=None)
        path = os.path.join(file, downloaded_name(media))
        with open(path, "wb") as f:
            f.write(b"media-bytes")
        return path


class Event:
    """Just enough of events.NewMessage.Event."""

    def __init__(self, message):
        self.message = message


def run(coro):
    return asyncio.run(coro)
