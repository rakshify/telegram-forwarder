"""Telegram destination: posts through the bot, with Telethon.

Everything Telegram-specific about posting lives here: the bot client, text
and caption limits, forum topics, upload attributes per media kind, polls,
and Telethon's error classes, which are translated into core.sink errors at
this module's boundary. Formatting goes through formatting.py.
"""
import datetime
import functools
import random
import time
from pathlib import Path
from typing import Optional

from telethon import errors, types
from telethon import utils as tl_utils

from ... import config
from ...core import rich as fmt
from ...core.model import Dest, Media, Poll
from ...core.sink import CantEdit, RateLimited, Rejected, Unreachable
from .formatting import to_entities
from .peers import resolve_peer

NAME = "Telegram"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
POLL_QUESTION_LIMIT = 300
POLL_OPTION_LIMIT = 100
POLL_MAX_OPTIONS = 10
FLOOD_SLEEP_S = 120  # Telethon sleeps through rate limits up to this long by itself


def telegram_waveform(waveform: bytes) -> Optional[bytes]:
    """Amplitudes of 0-100 (as WhatsApp sends them) -> Telegram's packed 5-bit samples (0-31)."""
    if not waveform:
        return None
    return tl_utils.encode_waveform(bytes(min(31, (b * 31 + 50) // 100) for b in waveform))


def _webp_to_gif(src: Path) -> Optional[Path]:
    """Animated stickers (animated WebP) -> GIF, which Telegram shows as an
    animation. None if the file isn't animated or can't be converted."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            if not getattr(im, "is_animated", False):
                return None
            dst = src.with_suffix(".gif")
            im.save(dst, format="GIF", save_all=True, loop=0, disposal=2)
            return dst
    except Exception:
        return None


def _translate_errors(method):
    """Telethon errors -> core.sink errors, so the pipeline needn't know Telethon."""
    @functools.wraps(method)
    async def wrapper(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except errors.FloodWaitError as e:
            raise RateLimited(e.seconds, NAME) from e
        except errors.ForbiddenError as e:  # bot removed, no rights
            raise Rejected(str(e), NAME) from e
        except errors.BadRequestError as e:  # bad input, or now and then an upload glitch
            raise Rejected(str(e), NAME, retry_once=True) from e
    return wrapper


class TelegramSink:
    name = NAME

    def __init__(self, session_base):
        self.session_base = session_base
        self.client = None

    @property
    def ready(self) -> bool:
        return self.client is not None

    async def start(self) -> None:
        self.client = config.make_client(self.session_base)
        await self.client.start(bot_token=config.BOT_TOKEN)
        self.client.flood_sleep_threshold = FLOOD_SLEEP_S

    async def stop(self) -> None:
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()

    async def resolve(self, dest: Dest):
        try:
            return await resolve_peer(self.client, int(dest.chat), dest.hash, role="dest", for_bot=True)
        except SystemExit as e:
            raise Unreachable(str(e).splitlines()[0], NAME, hint="Add the bot there") from None

    @staticmethod
    def _in_topic(dest: Dest, reply_to: Optional[int]) -> Optional[int]:
        """Without a message to reply to, post inside the destination's forum topic."""
        return reply_to if reply_to is not None else (dest.topic or None)

    @_translate_errors
    async def send_text(self, dest: Dest, text: fmt.Rich, reply_to: Optional[int]) -> Optional[int]:
        reply_to = self._in_topic(dest, reply_to)
        first = None
        for chunk in fmt.split(text, TEXT_LIMIT):
            sent = await self.client.send_message(
                dest.handle, chunk.text,
                formatting_entities=to_entities(chunk),
                parse_mode=None,
                reply_to=reply_to if first is None else (dest.topic or None),
            )
            first = first or sent.id
        return first

    @_translate_errors
    async def send_media(self, dest: Dest, media: Media, caption: fmt.Rich, path: Path,
                         reply_to: Optional[int]) -> int:
        reply_to = self._in_topic(dest, reply_to)
        kind, file = media.kind, path
        if kind == "sticker":
            gif = _webp_to_gif(path) if media.animated else None
            if gif is not None:
                kind, file = "gif", gif
            else:
                caption = fmt.Rich()  # stickers can't carry a caption

        chunks = fmt.split(caption, CAPTION_LIMIT, TEXT_LIMIT) if caption.text else []
        first = chunks[0] if chunks else fmt.Rich()
        common = dict(
            caption=first.text,
            formatting_entities=to_entities(first),
            parse_mode=None,
            reply_to=reply_to,
        )
        try:
            sent = await self._send_file_as(dest, media, kind, file, common)
        except errors.BadRequestError as e:
            if kind == "document":
                raise
            print(f"  Telegram didn't accept it as {kind} ({e}); sending it as a file instead.")
            sent = await self.client.send_file(dest.handle, str(file), force_document=True, **common)

        for chunk in chunks[1:]:  # caption longer than Telegram allows
            await self.client.send_message(
                dest.handle, chunk.text,
                formatting_entities=to_entities(chunk),
                parse_mode=None, reply_to=sent.id,
            )
        return sent.id

    async def _send_file_as(self, dest: Dest, media: Media, kind: str, file: Path, common: dict):
        send = self.client.send_file
        peer, f = dest.handle, str(file)
        if kind == "photo":
            return await send(peer, f, force_document=False, **common)
        if kind == "video":
            attrs = [types.DocumentAttributeVideo(duration=float(media.seconds), w=media.width,
                                                  h=media.height, supports_streaming=True)]
            return await send(peer, f, attributes=attrs, supports_streaming=True,
                              force_document=False, **common)
        if kind == "video_note":  # round video
            attrs = [types.DocumentAttributeVideo(duration=float(media.seconds), w=media.width,
                                                  h=media.height, round_message=True)]
            return await send(peer, f, attributes=attrs, video_note=True, **common)
        if kind == "animation":
            attrs = [types.DocumentAttributeVideo(duration=float(media.seconds), w=media.width,
                                                  h=media.height),
                     types.DocumentAttributeAnimated()]
            return await send(peer, f, attributes=attrs, force_document=False, **common)
        if kind == "voice":
            attrs = [types.DocumentAttributeAudio(duration=int(media.seconds), voice=True,
                                                  waveform=telegram_waveform(media.waveform))]
            return await send(peer, f, attributes=attrs, voice_note=True, **common)
        if kind == "audio":
            attrs = [types.DocumentAttributeAudio(duration=int(media.seconds), voice=False,
                                                  title=media.title or None,
                                                  performer=media.performer or None)]
            return await send(peer, f, attributes=attrs, force_document=False, **common)
        if kind == "document":
            attrs = [types.DocumentAttributeFilename(file_name=file.name)]
            return await send(peer, f, attributes=attrs, force_document=True, **common)
        if kind == "sticker":
            attrs = [types.DocumentAttributeSticker(alt="", stickerset=types.InputStickerSetEmpty())]
            return await send(peer, f, attributes=attrs, mime_type="image/webp",
                              force_document=False, **common)
        if kind == "gif":  # converted animated sticker
            return await send(peer, f, force_document=False, **common)
        raise ValueError(f"unknown media kind {kind!r}")

    @_translate_errors
    async def send_poll(self, dest: Dest, poll: Poll, reply_to: Optional[int]) -> Optional[int]:
        question = poll.question.strip()
        kept = [(i, o.strip()) for i, o in enumerate(poll.options) if o.strip()]
        options = [o for _, o in kept]
        fits = (
            2 <= len(options) <= POLL_MAX_OPTIONS
            and len(set(options)) == len(options)
            and 0 < fmt.utf16_len(question) <= POLL_QUESTION_LIMIT
            and all(fmt.utf16_len(o) <= POLL_OPTION_LIMIT for o in options)
        )
        if not fits:
            return None  # Telegram can't hold this poll; the pipeline posts it as text
        # A quiz needs its right answer; without it, post a regular poll.
        correct = next((n for n, (i, _) in enumerate(kept) if i == poll.correct), None)
        quiz = poll.quiz and correct is not None and not poll.multiple
        closing = {}
        if poll.close_period:
            closing["close_period"] = poll.close_period
        elif poll.close_date and poll.close_date > time.time() + 5:
            closing["close_date"] = datetime.datetime.fromtimestamp(poll.close_date, datetime.timezone.utc)
        tg_poll = types.Poll(
            id=random.getrandbits(62),
            question=types.TextWithEntities(text=question, entities=[]),
            answers=[types.PollAnswer(text=types.TextWithEntities(text=o, entities=[]),
                                      option=bytes([n]))
                     for n, o in enumerate(options)],
            hash=0,
            multiple_choice=poll.multiple or None,
            quiz=True if quiz else None,
            public_voters=poll.public or None,
            **closing,
        )
        media = types.InputMediaPoll(poll=tg_poll, correct_answers=[correct] if quiz else None)
        sent = await self.client.send_file(dest.handle, file=media, reply_to=self._in_topic(dest, reply_to))
        return sent.id

    @_translate_errors
    async def edit(self, dest: Dest, msg_id: int, text: fmt.Rich, is_media: bool) -> bool:
        text = fmt.truncate(text, CAPTION_LIMIT if is_media else TEXT_LIMIT)
        try:
            await self.client.edit_message(
                dest.handle, msg_id, text.text,
                formatting_entities=to_entities(text), parse_mode=None,
            )
        except errors.MessageNotModifiedError:
            return False
        except (errors.MessageIdInvalidError, errors.MessageAuthorRequiredError,
                errors.MessageEditTimeExpiredError) as e:
            raise CantEdit(str(e), NAME) from e
        return True

    async def notify(self, user_id: int, text: str) -> None:
        """Direct message to a person (alerts). They must have pressed Start on the bot."""
        await self.client.send_message(user_id, text, parse_mode=None)
