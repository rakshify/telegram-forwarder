"""Forward messages from N source chats to N destination chats (1:1 mapped).

Design notes
------------
* The user account (logged in via `login`) listens to source chats — only the
  user can listen to arbitrary chats they are a member of.
* The bot account sends to destination chats — sending as a bot keeps the user
  account out of the destination membership requirements and avoids surfacing
  the user's identity in destination groups.
* Per-pair `msg_id_map` stores the mapping {source_msg_id -> destination_msg_id}
  so replies in the source chat appear as proper replies in the destination.
* Media is downloaded by the user client and re-uploaded by the bot client.
  This preserves message content across two distinct accounts (the bot has no
  direct access to files uploaded in the source group).
"""
import asyncio
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telethon import TelegramClient, events, types
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    InputPeerChannel,
    InputPeerChat,
    DocumentAttributeFilename,
    MessageMediaPoll,
    MessageMediaWebPage,
)

from . import config


@dataclass
class ChatPair:
    """One source -> destination forwarding pair.

    `source_hash` / `dest_hash` of 0 marks the corresponding chat as a basic
    (legacy) group, which uses InputPeerChat (no access_hash). Anything else
    is treated as a supergroup/channel and uses InputPeerChannel.
    """
    source_id: int          # bare id (no -100 / no leading minus)
    source_hash: int        # access_hash, or 0 for basic groups
    dest_id: int            # bare id (no -100 / no leading minus)
    dest_hash: int          # access_hash, or 0 for basic groups
    source_peer: Optional[object] = None     # InputPeerChannel or InputPeerChat
    dest_peer: Optional[object] = None       # InputPeerChannel or InputPeerChat
    msg_id_map: Dict[int, int] = field(default_factory=dict)


async def _resolve_peer(
    client: TelegramClient,
    chat_id: int,
    access_hash: int,
    role: str,
    for_bot: bool,
):
    """Return a usable input-peer for `client`.

    Telegram access_hashes are *per-account* — the hash returned by
    `list-groups` is the user account's hash and is NOT valid for the bot.
    So:

      * source (user side): supergroups can be addressed directly by
        InputPeerChannel(chat_id, hash_from_list_groups). For basic groups,
        the user session needs to know the chat — so we get_entity it.
      * dest (bot side): we *must* let the bot resolve the entity itself, so
        the session gets the bot's own access_hash. This works uniformly for
        supergroups, channels, and basic groups. The user-provided -mh value
        is accepted by the CLI for symmetry with -gh but ignored at runtime.
    """
    if for_bot:
        # Always resolve via the bot's own session so we get the bot's hash.
        marked = -chat_id if access_hash == 0 else int(f"-100{chat_id}")
        try:
            return await client.get_entity(marked)
        except Exception as e:
            raise SystemExit(
                f"Bot could not resolve {role} chat (id={chat_id}): {e}\n"
                f"Make sure the bot is a member of that chat with permission to post."
            )

    # User side
    if access_hash != 0:
        return InputPeerChannel(channel_id=chat_id, access_hash=access_hash)

    try:
        return await client.get_entity(-chat_id)
    except Exception as e:
        raise SystemExit(
            f"Could not resolve basic-group {role} chat_id={chat_id} on the user session: {e}\n"
            f"Make sure your user account is a member of that group."
        )


def _build_peer(chat_id: int, access_hash: int):
    """Sync peer builder (kept for backward compatibility / direct use).

    Prefer `_resolve_peer` from inside the running event loop, since it also
    primes the session cache for basic groups.
    """
    if access_hash == 0:
        return InputPeerChat(chat_id=chat_id)
    return InputPeerChannel(channel_id=chat_id, access_hash=access_hash)


def parse_chat_id(s: str) -> int:
    """Accept -1001234567890, -1234567890, or 1234567890; return the bare id.

    Telegram supergroup/channel IDs are shown with a `-100` prefix; basic
    groups are shown with just a leading `-`. The internal id (what
    InputPeerChannel / InputPeerChat want) is the same number with both
    prefixes stripped.
    """
    s = s.strip()
    val = int(s)
    s_str = str(val)
    if s_str.startswith("-100"):
        return int(s_str[4:])
    if s_str.startswith("-"):
        return int(s_str[1:])
    return val


# ------------------------------------------------------------------ runtime

async def forward_pairs(pairs: List[ChatPair]) -> None:
    """Wire up handlers for every pair and run until disconnected."""
    config.validate(require_bot=True)

    user_client = TelegramClient(
        str(config.USER_SESSION_PATH),
        config.api_id_int(),
        config.TG_API_HASH,
    )
    bot_client = TelegramClient(
        str(config.BOT_SESSION_PATH),
        config.api_id_int(),
        config.TG_API_HASH,
    )

    await user_client.connect()
    if not await user_client.is_user_authorized():
        print("User session not authorized. Run `login` first.")
        await user_client.disconnect()
        return

    await bot_client.start(bot_token=config.BOT_TOKEN)

    # Build the right input-peer type for every endpoint.
    # See _resolve_peer for the per-account access_hash subtlety.
    for pair in pairs:
        pair.source_peer = await _resolve_peer(
            user_client, pair.source_id, pair.source_hash,
            role="source", for_bot=False,
        )
        pair.dest_peer = await _resolve_peer(
            bot_client, pair.dest_id, pair.dest_hash,
            role="dest", for_bot=True,
        )

    # Register one handler per source chat, capturing the right pair
    for pair in pairs:
        _register_handler(user_client, bot_client, pair)
        print(f"Registered: source={pair.source_id} -> dest={pair.dest_id}")

    print("Listening. Press Ctrl+C to stop.")
    try:
        await user_client.run_until_disconnected()
    finally:
        if bot_client.is_connected():
            await bot_client.disconnect()


def _register_handler(
    user_client: TelegramClient, bot_client: TelegramClient, pair: ChatPair
) -> None:

    @user_client.on(events.NewMessage(chats=pair.source_peer))
    async def handler(event: events.NewMessage.Event):
        try:
            await _forward_one(bot_client, pair, event.message)
        except Exception as e:
            mid = getattr(event.message, "id", "?")
            print(f"[{pair.source_id} -> {pair.dest_id}] error on msg {mid}: {e}")


# ------------------------------------------------------------------ the work

async def _forward_one(
    bot_client: TelegramClient, pair: ChatPair, msg: Message
) -> None:
    """Re-send `msg` into `pair.dest_peer` via the bot, preserving as much as we can."""

    # Resolve reply target via the per-pair id map
    reply_to: Optional[int] = None
    if msg.reply_to_msg_id:
        reply_to = pair.msg_id_map.get(msg.reply_to_msg_id)

    sent = None

    # 1) Polls — re-construct because the original poll object is bound to the
    # source chat's poll-id and cannot be re-sent verbatim by a different sender.
    if msg.media and isinstance(msg.media, MessageMediaPoll):
        poll = msg.media.poll
        constructed = types.Poll(
            id=random.getrandbits(63),
            question=poll.question,
            answers=[
                types.PollAnswer(text=a.text, option=a.option) for a in poll.answers
            ],
            multiple_choice=poll.multiple_choice,
            public_voters=poll.public_voters,
            quiz=poll.quiz,
            close_period=poll.close_period,
            close_date=poll.close_date,
        )
        sent = await bot_client.send_file(
            pair.dest_peer,
            file=types.InputMediaPoll(poll=constructed),
            reply_to=reply_to,
        )

    # 2) Media (photo, video, audio, voice, video-note, sticker, gif, document).
    #    Download via user client, re-upload via bot. Skip pure web-page previews
    #    so they fall through to the text path.
    elif msg.media and not isinstance(msg.media, MessageMediaWebPage):
        file_bytes = await msg.download_media(file=bytes)
        if file_bytes:
            send_kwargs = dict(
                entity=pair.dest_peer,
                file=file_bytes,
                caption=msg.message or "",
                reply_to=reply_to,
                formatting_entities=msg.entities,
            )

            # Hint the right media type to the bot
            if msg.voice:
                send_kwargs["voice_note"] = True
            elif msg.video_note:
                send_kwargs["video_note"] = True
            elif msg.gif:
                send_kwargs["attributes"] = [
                    DocumentAttributeFilename(file_name="animation.mp4")
                ]
            elif msg.photo:
                send_kwargs["attributes"] = [
                    DocumentAttributeFilename(file_name="image.png")
                ]
            elif msg.sticker:
                # Bots can't always send arbitrary stickers; fall back to file.
                send_kwargs["attributes"] = [
                    DocumentAttributeFilename(file_name="sticker.webp")
                ]
            elif msg.video:
                send_kwargs["attributes"] = [
                    DocumentAttributeFilename(file_name="video.mp4")
                ]
            elif msg.audio:
                send_kwargs["attributes"] = [
                    DocumentAttributeFilename(file_name="audio.mp3")
                ]
            elif msg.document:
                # Preserve the original filename for plain documents
                fname = next(
                    (
                        a.file_name
                        for a in msg.document.attributes
                        if isinstance(a, DocumentAttributeFilename)
                    ),
                    None,
                )
                if fname:
                    send_kwargs["attributes"] = [
                        DocumentAttributeFilename(file_name=fname)
                    ]

            sent = await bot_client.send_file(**send_kwargs)

    # 3) Plain text (covers text-only messages and text-with-link-preview).
    if sent is None:
        text = msg.message or ""
        if not text:
            # Service messages, empty payloads, etc — nothing useful to forward.
            return
        sent = await bot_client.send_message(
            entity=pair.dest_peer,
            message=text,
            reply_to=reply_to,
            formatting_entities=msg.entities,
        )

    pair.msg_id_map[msg.id] = sent.id
    print(f"[{pair.source_id} -> {pair.dest_id}] {msg.id} -> {sent.id}")
