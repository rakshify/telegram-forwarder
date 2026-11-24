"""Telegram as a source: a user account's chosen chats (or forum topics) feed the pipeline.

What it does (the pipeline, src/core/pipeline.py, does the rest):
* connects with the user's session (made by `login`) and resolves each
  source chat;
* catches up on what arrived while it was down. From each chat's catch-up
  point (store.cursors) it fetches the newer messages and queues them, in
  order. A chat seen for the first time starts at its latest message,
  without backfilling its history;
* queues each new message as it arrives. Its live handler is registered
  before catch-up starts and holds what comes in meanwhile until catch-up is
  done, so nothing falls in between and the order is kept;
* turns a queued message back into a neutral Message (convert.py) and
  downloads its media, fetching the message again if Telegram's file
  reference for it has expired.

A queued entry is the whole Telegram message (its TL serialization) plus the
sender's display name, so it posts the same after a restart as it would
have at once.
"""
import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from telethon import errors, events
from telethon.extensions import BinaryReader

from ... import config, state, users
from ...core.model import Message, Skip
from ...core.source import Host, MediaUnavailable, NotConnected
from ...core.store import InboxItem
from . import convert
from .peers import resolve_peer

NAME = "Telegram"
DISCONNECT_ALERT_S = 600


@dataclass(frozen=True)
class Chat:
    """A Telegram source chat, or one forum topic of it."""
    chat_id: int      # bare id (no -100)
    access_hash: int  # 0 only for an old-style basic group
    topic: int = 0    # 0: the whole chat

    @property
    def key(self) -> str:
        """Its name in the pipeline (Pair.source): 1234567890, or 1234567890#17 for a topic."""
        return f"{self.chat_id}#{self.topic}" if self.topic else str(self.chat_id)


class TelegramSource:
    name = NAME

    def __init__(self, user: users.UserRecord, chats: List[Chat]):
        self.user = user
        self.chats = list(dict.fromkeys(chats))  # unique, in config order
        self.by_id: Dict[int, List[Chat]] = {}
        for chat in self.chats:
            self.by_id.setdefault(chat.chat_id, []).append(chat)
        self.client = None
        self.peers: Dict[int, object] = {}       # chat id -> input peer for the user session
        self.halted: Optional[str] = None
        self._host: Optional[Host] = None
        self._catching_up = True
        self._held: list = []                    # live messages that came during catch-up
        self._down_since: Optional[float] = None
        self._legacy: Optional[Dict[str, int]] = None

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected()

    def attach(self, host: Host) -> None:
        self._host = host

    def label(self, chat: str) -> str:
        return chat

    # ================================================================ lifecycle

    async def start(self) -> None:
        client = config.make_client(self.user.session_base)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            await self._halt("not-authorized", f"the Telegram session of user {self.user.short_id} "
                             "isn't logged in any more. Run `login` again, then restart the forwarder.")
            return
        self.client = client
        await self._resolve_chats()

        client.add_event_handler(self._on_new_message,
                                 events.NewMessage(chats=list(self.peers.values())))
        for chat in self.chats:
            if chat.chat_id in self.peers:
                await self._catch_up(chat)
        self._catching_up = False
        held, self._held = self._held, []
        for msg in held:
            self._queue(msg)

        for chat in self.chats:
            if chat.chat_id in self.peers:
                print(f"Registered: source={chat.key} -> {self._host.dests_label(chat.key)}")
        print("Listening.")
        client.disconnected.add_done_callback(self._on_disconnected)

    async def stop(self) -> None:
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()

    def _on_disconnected(self, _future) -> None:
        if self._host.stopping or self.halted:
            return
        print("Telegram (user session) disconnected for good. Exiting so Docker restarts the forwarder.")
        self._host.request_stop(exit_code=1)

    async def check(self) -> None:
        if self.halted or self.client is None:
            return
        if self.client.is_connected():
            self._down_since = None
            return
        now = time.time()
        if self._down_since is None:
            self._down_since = now
        elif now - self._down_since > DISCONNECT_ALERT_S:
            await self._host.alert("disconnected", f"Telegram (user {self.user.short_id}) has been "
                                   f"disconnected for {int((now - self._down_since) / 60)} minutes. "
                                   f"It keeps retrying by itself.")

    async def _halt(self, key: str, text: str) -> None:
        if self.halted:
            return
        self.halted = text
        print(f"Telegram side stopped: {text}")
        await self._host.alert(key, text)

    async def _resolve_chats(self) -> None:
        primed = False

        async def prime():
            # Some chats (basic groups, never-opened communities) need the
            # session's entity cache primed; iter_dialogs fills it.
            nonlocal primed
            if not primed:
                async for _ in self.client.iter_dialogs():
                    pass
                primed = True

        for chat_id, chats in self.by_id.items():
            try:
                self.peers[chat_id] = await resolve_peer(self.client, chat_id, chats[0].access_hash,
                                                         role="source", for_bot=False, prime=prime)
            except SystemExit as e:
                reason = str(e).splitlines()[0]
                print(f"  WARNING: {reason}")
                await self._host.alert(f"source:{chat_id}", f"can't read Telegram chat {chat_id} "
                                       f"({reason}), so nothing is forwarded from it. Fix the config "
                                       f"or join the chat, then restart the forwarder.")
        # Even with access hashes, fetching history needs the chats in the cache.
        if not primed and self.peers:
            print("  priming user session cache for catchup...")
            await prime()

    # ================================================================ catch-up and live

    async def _catch_up(self, chat: Chat) -> None:
        store = self._host.store
        last = store.cursor(chat.key)
        if last is None and not self._host.dry_run:  # a dry run reads and writes no state
            last = self._legacy_cursor(chat)
            if last:
                store.set_cursor(chat.key, last)
                print(f"[{chat.key}] carried over the catch-up point {last} from forward_state")
        peer = self.peers[chat.chat_id]
        scope = {"reply_to": chat.topic} if chat.topic else {}
        if not last:
            latest = await self.client.get_messages(peer, limit=1, **scope)
            baseline = latest[0].id if latest else 0
            store.set_cursor(chat.key, baseline)
            what = f"topic {chat.topic}" if chat.topic else "whole chat"
            print(f"[{chat.key}] no prior state ({what}) — baselined at message id {baseline}")
            return
        missed = [m async for m in self.client.iter_messages(peer, min_id=last, **scope)]
        if not missed:
            print(f"[{chat.key}] no missed messages since id {last}")
            return
        missed.reverse()
        print(f"[{chat.key}] catching up {len(missed)} missed message(s)")
        for m in missed:
            self._queue(m, only=chat)

    def _legacy_cursor(self, chat: Chat) -> Optional[int]:
        """The catch-up point the old forwarder kept in forward_state_<short_id>.json,
        one per pair (<short_id>:<source>:<dest>[:<topic>]): the newest of this chat's."""
        if self._legacy is None:
            self._legacy = state.load_state(self.user.short_id)
        best = None
        for key, last_id in self._legacy.items():
            parts = key.split(":")
            if len(parts) < 3 or parts[1] != str(chat.chat_id):
                continue
            if (int(parts[3]) if len(parts) > 3 else 0) != chat.topic:
                continue
            best = max(best or 0, int(last_id))
        return best

    async def _on_new_message(self, event) -> None:
        # No awaits: handlers start in arrival order, so the queue keeps Telegram's.
        if self._catching_up:
            self._held.append(event.message)
            return
        self._queue(event.message)

    def _chats_for(self, msg) -> List[Chat]:
        topic = convert.topic_of(msg)
        return [c for c in self.by_id.get(convert.chat_id_of(msg.peer_id), [])
                if not c.topic or c.topic == topic]

    def _queue(self, msg, only: Optional[Chat] = None) -> None:
        store = self._host.store
        payload = None
        for chat in [only] if only is not None else self._chats_for(msg):
            if msg.id <= (store.cursor(chat.key) or 0):
                continue  # queued already (catch-up and the live handler overlap)
            if payload is None:
                payload = json.dumps({
                    "v": 1,
                    "sender": convert.sender_display(msg.sender),
                    "tl": base64.b64encode(bytes(msg)).decode("ascii"),
                }).encode()
            self._host.enqueue(chat.key, str(msg.id), payload)
            store.set_cursor(chat.key, msg.id)

    # ================================================================ messages

    def decode(self, item: InboxItem) -> Union[Message, Skip]:
        data = json.loads(item.raw)
        msg = BinaryReader(base64.b64decode(data["tl"])).tgread_object()
        return convert.to_message(msg, data.get("sender") or "")

    async def download(self, msg: Message, folder: Path) -> Path:
        media: convert.TgMedia = msg.media
        if not self.connected:
            raise NotConnected()
        try:
            try:
                path = await self.client.download_media(media.ref, file=str(folder))
            except errors.FileReferenceExpiredError:
                peer = self.peers.get(media.chat_id, media.chat_id)
                fresh = await self.client.get_messages(peer, ids=media.msg_id)
                if fresh is None or fresh.media is None:
                    raise MediaUnavailable("the message is gone from Telegram")
                path = await self.client.download_media(fresh.media, file=str(folder))
        except errors.BadRequestError as e:
            raise MediaUnavailable(str(e)) from e
        if not path:
            raise MediaUnavailable("Telegram had nothing to download")
        return Path(path)
