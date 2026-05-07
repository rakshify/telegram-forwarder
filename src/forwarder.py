"""Forward messages from N source chats to N destination chats (1:1 mapped).

Per-user. Each invocation runs against one user session (selected by `-u`)
and one bot. The forwarder also persists a "last forwarded message id" per
pair, so when it restarts after downtime it backfills any messages that
arrived while it was offline before going live.

Design notes
------------
* The user account (logged in via `login`) listens to source chats — only
  the user can listen to arbitrary chats they are a member of.
* The bot account sends to destination chats — keeps the user's identity
  out of destination groups.
* Per-pair `msg_id_map` stores {source_msg_id -> destination_msg_id} so
  replies in the source chat appear as proper replies in the destination.
* Media is downloaded by the user client and re-uploaded by the bot client.
* Forward state (forward_state.json) holds the last successfully forwarded
  source message id for every pair. On startup, we fetch every newer
  message via `iter_messages(min_id=last_seen)` and forward in chronological
  order before attaching the live listener.
"""
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

from . import config, state, users


@dataclass
class ChatPair:
    """One source -> destination forwarding pair.

    `source_hash` / `dest_hash` of 0 marks the corresponding chat as a basic
    (legacy) group, which uses InputPeerChat (no access_hash). Anything else
    is treated as a supergroup/channel and uses InputPeerChannel.

    `topic_id` of 0 means "the whole supergroup, no topic filter". Any other
    value filters source messages to that one topic of a forum supergroup.

    `dest_topic_id` of 0 means "post in the destination's main feed". Any
    other value posts inside that destination topic (the destination must be
    a forum-enabled supergroup, and the topic must already exist).
    """
    source_id: int
    source_hash: int
    dest_id: int
    dest_hash: int
    topic_id: int = 0
    dest_topic_id: int = 0
    source_peer: Optional[object] = None
    dest_peer: Optional[object] = None
    msg_id_map: Dict[int, int] = field(default_factory=dict)


def parse_chat_id(s: str) -> int:
    """Accept -1001234567890, -1234567890, or 1234567890; return the bare id."""
    s = s.strip()
    val = int(s)
    s_str = str(val)
    if s_str.startswith("-100"):
        return int(s_str[4:])
    if s_str.startswith("-"):
        return int(s_str[1:])
    return val


# ------------------------------------------------------------------ runtime

async def forward_pairs(short_id: str, pairs: List[ChatPair]) -> None:
    """Wire up handlers for every pair, run catch-up, then listen forever."""
    config.validate(require_bot=True)
    record = users.get_user(short_id)

    user_client = TelegramClient(
        str(record.session_base), config.api_id_int(), config.TG_API_HASH
    )
    bot_client = TelegramClient(
        str(config.BOT_SESSION_PATH), config.api_id_int(), config.TG_API_HASH
    )

    await user_client.connect()
    if not await user_client.is_user_authorized():
        print(f"User {short_id} session not authorized. Run `login` again.")
        await user_client.disconnect()
        return

    await bot_client.start(bot_token=config.BOT_TOKEN)

    # Resolve every endpoint to a usable peer for the relevant client
    for pair in pairs:
        pair.source_peer = await _resolve_peer(
            user_client, pair.source_id, pair.source_hash,
            role="source", for_bot=False,
        )
        pair.dest_peer = await _resolve_peer(
            bot_client, pair.dest_id, pair.dest_hash,
            role="dest", for_bot=True,
        )

    # Catch up missed messages for every pair before going live
    forward_state = state.load_state()
    for pair in pairs:
        await _catchup(user_client, bot_client, pair, short_id, forward_state)

    # Now register live handlers
    for pair in pairs:
        _register_handler(user_client, bot_client, pair, short_id, forward_state)
        print(f"Registered: source={pair.source_id}{_topic_label(pair)} "
              f"-> dest={pair.dest_id}")

    print("Listening. Press Ctrl+C to stop.")
    try:
        await user_client.run_until_disconnected()
    finally:
        if bot_client.is_connected():
            await bot_client.disconnect()


# ------------------------------------------------------------------ peer resolution

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


# ------------------------------------------------------------------ catch-up

async def _catchup(
    user_client: TelegramClient,
    bot_client: TelegramClient,
    pair: ChatPair,
    short_id: str,
    forward_state: Dict[str, int],
) -> None:
    """Forward any messages that arrived in this pair's source while the
    forwarder was offline.

    First-ever run for a pair: establish the baseline at the current latest
    message id (no full-history backfill — only catch up from this point on).
    """
    skey = state.state_key(short_id, pair.source_id, pair.dest_id, pair.topic_id)
    last_seen = forward_state.get(skey, 0)

    # iter_messages takes reply_to=<topic_top_msg_id> to filter to one forum topic.
    iter_kwargs = {"min_id": last_seen}
    latest_kwargs = {"limit": 1}
    if pair.topic_id:
        iter_kwargs["reply_to"] = pair.topic_id
        latest_kwargs["reply_to"] = pair.topic_id

    if last_seen == 0:
        latest_msgs = await user_client.get_messages(pair.source_peer, **latest_kwargs)
        baseline = latest_msgs[0].id if latest_msgs else 0
        forward_state[skey] = baseline
        state.save_state(forward_state)
        scope = f"topic {pair.topic_id}" if pair.topic_id else "whole chat"
        print(f"[{pair.source_id}{_topic_label(pair)} -> {pair.dest_id}] "
              f"no prior state ({scope}) — baselined at message id {baseline}")
        return

    missed: List[Message] = []
    async for m in user_client.iter_messages(pair.source_peer, **iter_kwargs):
        missed.append(m)
    missed.reverse()

    if not missed:
        print(f"[{pair.source_id}{_topic_label(pair)} -> {pair.dest_id}] "
              f"no missed messages since id {last_seen}")
        return

    print(f"[{pair.source_id}{_topic_label(pair)} -> {pair.dest_id}] "
          f"catching up {len(missed)} missed message(s)")
    for m in missed:
        try:
            await _forward_one(bot_client, pair, m)
        except Exception as e:
            print(f"  catchup error on msg {m.id}: {e} — "
                  f"stopping catchup for this pair, will retry next run")
            return
        forward_state[skey] = m.id
        state.save_state(forward_state)


def _topic_label(pair: ChatPair) -> str:
    return f"#{pair.topic_id}" if pair.topic_id else ""


# ------------------------------------------------------------------ live listener

def _register_handler(
    user_client: TelegramClient,
    bot_client: TelegramClient,
    pair: ChatPair,
    short_id: str,
    forward_state: Dict[str, int],
) -> None:
    skey = state.state_key(short_id, pair.source_id, pair.dest_id, pair.topic_id)

    @user_client.on(events.NewMessage(chats=pair.source_peer))
    async def handler(event: events.NewMessage.Event):
        msg = event.message
        try:
            # Topic filter: a message belongs to topic T if its reply_to header
            # has reply_to_top_id == T (replies inside the topic) OR
            # reply_to_msg_id == T (the topic's own root message, or a direct
            # reply to it). Messages in the General topic / non-forum chats
            # have no reply_to header at all, which we treat as topic_id=0.
            if pair.topic_id:
                rt = msg.reply_to
                if rt is None:
                    return
                top = getattr(rt, "reply_to_top_id", None) or rt.reply_to_msg_id
                if top != pair.topic_id:
                    return

            # Skip if catch-up already handled this id (overlap window between
            # catch-up finishing and the listener attaching).
            if msg.id <= forward_state.get(skey, 0):
                return
            await _forward_one(bot_client, pair, msg)
            forward_state[skey] = msg.id
            state.save_state(forward_state)
        except Exception as e:
            mid = getattr(msg, "id", "?")
            print(f"[{pair.source_id}{_topic_label(pair)} -> {pair.dest_id}] "
                  f"error on msg {mid}: {e}")


# ------------------------------------------------------------------ the actual send

async def _forward_one(
    bot_client: TelegramClient, pair: ChatPair, msg: Message
) -> None:
    """Re-send `msg` into `pair.dest_peer` via the bot, preserving as much as we can."""

    # Resolve reply target. Order of preference:
    #   1) If the source message is a reply and we've forwarded the parent
    #      before, link to the corresponding destination message id.
    #   2) Otherwise, if a destination topic is configured, use that topic's
    #      top-message id so the message lands inside that topic.
    #   3) Otherwise None — message goes to the destination's main feed.
    reply_to: Optional[int] = None
    if msg.reply_to_msg_id:
        reply_to = pair.msg_id_map.get(msg.reply_to_msg_id)
    if reply_to is None and pair.dest_topic_id:
        reply_to = pair.dest_topic_id

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
            if msg.voice:
                send_kwargs["voice_note"] = True
            elif msg.video_note:
                send_kwargs["video_note"] = True
            elif msg.gif:
                send_kwargs["attributes"] = [DocumentAttributeFilename(file_name="animation.mp4")]
            elif msg.photo:
                send_kwargs["attributes"] = [DocumentAttributeFilename(file_name="image.png")]
            elif msg.sticker:
                send_kwargs["attributes"] = [DocumentAttributeFilename(file_name="sticker.webp")]
            elif msg.video:
                send_kwargs["attributes"] = [DocumentAttributeFilename(file_name="video.mp4")]
            elif msg.audio:
                send_kwargs["attributes"] = [DocumentAttributeFilename(file_name="audio.mp3")]
            elif msg.document:
                fname = next(
                    (a.file_name for a in msg.document.attributes
                     if isinstance(a, DocumentAttributeFilename)),
                    None,
                )
                if fname:
                    send_kwargs["attributes"] = [DocumentAttributeFilename(file_name=fname)]

            sent = await bot_client.send_file(**send_kwargs)

    # 3) Plain text (covers text-only messages and text-with-link-preview).
    if sent is None:
        text = msg.message or ""
        if not text:
            return
        sent = await bot_client.send_message(
            entity=pair.dest_peer,
            message=text,
            reply_to=reply_to,
            formatting_entities=msg.entities,
        )

    pair.msg_id_map[msg.id] = sent.id
    print(f"[{pair.source_id} -> {pair.dest_id}] {msg.id} -> {sent.id}")
