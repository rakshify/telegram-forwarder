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
    MessageEntityBold,
    User,
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

    `attribution` controls whether each forwarded message is prefixed with
    the source sender's name (and @username, when present). Off by default
    so the original behavior is unchanged.
    """
    source_id: int
    source_hash: int
    dest_id: int
    dest_hash: int
    topic_id: int = 0
    dest_topic_id: int = 0
    attribution: bool = False
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
        str(config.bot_session_path(short_id)), config.api_id_int(), config.TG_API_HASH
    )

    await user_client.connect()
    if not await user_client.is_user_authorized():
        print(f"User {short_id} session not authorized. Run `login` again.")
        await user_client.disconnect()
        return

    await bot_client.start(bot_token=config.BOT_TOKEN)

    # Some entities (basic groups, never-opened communities) require the
    # session's entity cache to be primed before get_entity will succeed.
    # iter_dialogs walks every chat the account is in and populates that
    # cache as a side effect.
    user_primed = {"done": False}
    bot_primed = {"done": False}

    async def _prime(client, primed_state):
        if primed_state["done"]:
            return
        async for _ in client.iter_dialogs():
            pass
        primed_state["done"] = True

    # Resolve every endpoint to a usable peer for the relevant client
    for pair in pairs:
        pair.source_peer = await _resolve_peer(
            user_client, pair.source_id, pair.source_hash,
            role="source", for_bot=False,
            prime=lambda: _prime(user_client, user_primed),
        )
        pair.dest_peer = await _resolve_peer(
            bot_client, pair.dest_id, pair.dest_hash,
            role="dest", for_bot=True,
            prime=lambda: _prime(bot_client, bot_primed),
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
    prime=None,
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

    `prime` is an optional async callable that primes the client's entity
    cache via iter_dialogs. We call it on demand the first time get_entity
    fails — saves the cost on subsequent lookups.
    """
    async def _get_with_prime(marked):
        try:
            return await client.get_entity(marked)
        except Exception as first_error:
            if prime is None:
                raise
            await prime()
            try:
                return await client.get_entity(marked)
            except Exception:
                raise first_error  # surface the original error message

    if for_bot:
        marked = -chat_id if access_hash == 0 else int(f"-100{chat_id}")
        try:
            return await _get_with_prime(marked)
        except Exception as e:
            raise SystemExit(
                f"Bot could not resolve {role} chat (id={chat_id}): {e}\n"
                f"Make sure the bot is a member of that chat with permission to post."
            )

    # User side
    if access_hash != 0:
        return InputPeerChannel(channel_id=chat_id, access_hash=access_hash)

    try:
        return await _get_with_prime(-chat_id)
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


# ------------------------------------------------------------------ attribution

def _sender_display(sender) -> str:
    """Render a sender as 'First Last (@username)' / 'First (@username)' /
    'First Last' / 'First' / '(unknown user)' depending on what's available.
    """
    if sender is None:
        return "(unknown user)"
    if not isinstance(sender, User):
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


def _format_attribution(msg: Message, original_text: str, original_entities):
    """Return (text, entities) with a bolded sender prefix prepended.

    Used when pair.attribution is True. The prefix renders as:

        Alex Doe (@alex_doe):
        <blank line>
        <original text>

    All of the original entities (bold/italic/links/etc.) are preserved by
    cloning each one with a shifted offset. We copy rather than mutate so
    re-using the same source message in another pair stays safe.
    """
    import copy

    sender = msg.sender                # populated by Telethon when the message arrives
    display = _sender_display(sender)
    prefix = f"{display}:\n\n"
    new_text = prefix + (original_text or "")

    # Bold the name portion only — everything up to (but not including) the colon.
    name_len = len(display)
    new_entities = [MessageEntityBold(offset=0, length=name_len)]

    # Shift original entities by the prefix length so they still cover the
    # right slice of new_text. copy.copy is enough — entities are flat objects.
    if original_entities:
        prefix_len = len(prefix)
        for ent in original_entities:
            shifted = copy.copy(ent)
            shifted.offset = ent.offset + prefix_len
            new_entities.append(shifted)

    return new_text, new_entities


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
    #    Download to a tempfile (with the right extension) so Telethon's
    #    server-side type detection works correctly, then re-upload via the
    #    bot. Passing raw bytes via file=<bytes> leaves Telegram unable to
    #    classify the upload and you end up with "tap to download" tiles
    #    instead of inline previews.
    elif msg.media and not isinstance(msg.media, MessageMediaWebPage):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            # download_media with a directory path lets Telethon pick the
            # right filename + extension for us based on the media type.
            downloaded = await msg.download_media(file=tmpdir)
            if not downloaded:
                pass  # nothing to forward (e.g. unsupported media type)
            else:
                caption_text = msg.message or ""
                caption_entities = msg.entities
                if pair.attribution:
                    caption_text, caption_entities = _format_attribution(
                        msg, caption_text, caption_entities
                    )
                send_kwargs = dict(
                    entity=pair.dest_peer,
                    file=downloaded,    # path with proper extension
                    caption=caption_text,
                    reply_to=reply_to,
                    formatting_entities=caption_entities,
                )

                if msg.voice:
                    # Voice notes: small audio bubble with waveform + play button.
                    send_kwargs["voice_note"] = True
                elif msg.video_note:
                    # Round video bubble.
                    send_kwargs["video_note"] = True
                elif msg.photo:
                    # Inline photo with preview. The .jpg extension on the
                    # downloaded path lets Telethon route this through the
                    # photo upload path rather than treating it as a document.
                    send_kwargs["force_document"] = False
                elif msg.gif:
                    # Animation — mp4 bytes recognized as a GIF when video=True
                    # and force_document=False is set.
                    send_kwargs["video"] = True
                    send_kwargs["force_document"] = False
                elif msg.video:
                    # Inline video with thumbnail + scrubber. supports_streaming
                    # lets Telegram start playback before download finishes.
                    send_kwargs["video"] = True
                    send_kwargs["supports_streaming"] = True
                    send_kwargs["force_document"] = False
                elif msg.audio:
                    # Music-style audio player (rather than a generic file tile).
                    send_kwargs["force_document"] = False
                    # Preserve original title/performer/duration if present.
                    from telethon.tl.types import DocumentAttributeAudio
                    audio_attr = next(
                        (a for a in msg.document.attributes
                         if isinstance(a, DocumentAttributeAudio)),
                        None,
                    )
                    if audio_attr is not None:
                        send_kwargs["attributes"] = [audio_attr]
                elif msg.sticker:
                    # Bots have limited sticker-send capability; fall back to
                    # webp file. Known limitation.
                    pass  # filename from download already ends in .webp
                elif msg.document:
                    # Generic document — preserve original filename so the
                    # destination shows the right extension and icon.
                    fname = next(
                        (a.file_name for a in msg.document.attributes
                         if isinstance(a, DocumentAttributeFilename)),
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
            return
        entities = msg.entities
        if pair.attribution:
            text, entities = _format_attribution(msg, text, entities)
        sent = await bot_client.send_message(
            entity=pair.dest_peer,
            message=text,
            reply_to=reply_to,
            formatting_entities=entities,
        )

    pair.msg_id_map[msg.id] = sent.id
    print(f"[{pair.source_id} -> {pair.dest_id}] {msg.id} -> {sent.id}")
