"""WhatsApp as a source: a linked device whose chosen chats feed the pipeline.

What it does (the pipeline, src/core/pipeline.py, does the rest):
* connects as a linked device (neonize, through client.py) and handles
  WhatsApp's session events. When WhatsApp ends the session (logged out,
  temporary ban, client too old, device unlinked) it alerts and idles
  instead of exiting, so Docker's restart policy doesn't turn it into a
  crash loop;
* queues each event from a configured chat right away, because WhatsApp
  delivers a message to a linked device only once, and notes the sender's
  profile name for @mentions and quotes;
* decodes a queued event into a neutral Message (convert.py reads the
  content) and downloads its media.

It never sends anything to WhatsApp, never marks chats as read and never
sets a presence, so the phone keeps getting its notifications.
"""
import asyncio
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from ...core.model import Message, Quote, Skip
from ...core.source import Host, MediaUnavailable, NotConnected
from ...core.store import InboxItem
from . import accounts, convert
from . import client as wa

NAME = "WhatsApp"
DOWNLOAD_TRIES = 3
DISCONNECT_ALERT_S = 600

_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/3gpp": ".3gp", "video/quicktime": ".mov",
    "audio/ogg": ".ogg", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".aac",
    "audio/amr": ".amr", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "application/pdf": ".pdf",
}
_DEFAULT_NAME = {
    "photo": "photo.jpg", "video": "video.mp4", "animation": "animation.mp4",
    "voice": "voice.ogg", "audio": "audio.mp3", "sticker": "sticker.webp",
    "document": "file",
}


def _safe_filename(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 120 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:120]
    return name


def media_filename(spec: convert.Media) -> str:
    """Name for the downloaded file. The extension matters: sinks (Telegram's
    type detection, Signal's attachment type) go by it."""
    mime = (spec.mimetype or "").split(";")[0].strip().lower()
    ext = _EXT.get(mime) or (mimetypes.guess_extension(mime) if mime else None) or ""
    if spec.kind == "document":
        name = _safe_filename(spec.filename)
        if not name:
            return "file" + ext
        if ext and "." not in name:
            name += ext
        return name
    default = _DEFAULT_NAME[spec.kind]
    if ext and spec.kind in ("photo", "audio"):
        return default.rsplit(".", 1)[0] + ext
    return default


class WhatsAppSource:
    name = NAME

    def __init__(self, account: accounts.WaAccount, chats: List[str]):
        self.account = account
        self.chats = list(dict.fromkeys(chats))  # configured source chats, in config order
        self._chat_set = set(self.chats)
        self.aliases: Dict[str, str] = {}        # other id of a 1:1 chat -> configured chat
        self.chat_names: Dict[str, str] = {}     # chat -> WhatsApp group name (for logs)
        self.my_name = account.push_name or ""
        self.my_jids = {account.jid} if account.jid else set()
        self.wa = None
        self.connected = False
        self.disconnected_since: Optional[float] = None
        self.halted: Optional[str] = None
        self.debug = bool(os.environ.get("WA_DEBUG"))  # log every incoming event
        self._first_connect = True
        self._host: Optional[Host] = None

    def attach(self, host: Host) -> None:
        self._host = host

    # ================================================================ lifecycle

    async def start(self) -> None:
        if wa.stored_device(self.account.db_path) is None:
            await self._halt("not-linked", "this WhatsApp account isn't linked. "
                             "Link it with wa-link, then restart the bridge.")
            return
        self.wa = wa.make_client(self.account.db_path)
        wa.on(self.wa, wa.MessageEv, self._on_message)
        wa.on(self.wa, wa.ConnectedEv, self._on_connected)
        wa.on(self.wa, wa.DisconnectedEv, self._on_disconnected)
        wa.on(self.wa, wa.LoggedOutEv, self._on_logged_out)
        wa.on(self.wa, wa.TemporaryBanEv, self._on_temp_ban)
        wa.on(self.wa, wa.ClientOutdatedEv, self._on_outdated)
        wa.on(self.wa, wa.StreamReplacedEv, self._on_stream_replaced)
        wa.on(self.wa, wa.ConnectFailureEv, self._on_connect_failure)
        wa.on(self.wa, wa.KeepAliveTimeoutEv, self._on_keepalive_timeout)
        wa.on(self.wa, wa.OfflineSyncCompletedEv, self._on_offline_sync)
        wa.on(self.wa, wa.PushNameSettingEv, self._on_push_name_setting)
        wa.on_qr(self.wa, self._on_unpaired)
        task = await self.wa.connect()
        task.add_done_callback(self._on_session_ended)

    async def stop(self) -> None:
        if self.wa is not None:
            await wa.stop(self.wa)

    def _on_session_ended(self, task: asyncio.Task) -> None:
        if self._host.stopping or self.halted or task.cancelled():
            return
        err = task.exception()
        if err is not None:
            print(f"WhatsApp session ended with an error: {err}. Exiting so Docker restarts the bridge.")
            self._host.request_stop(exit_code=1)

    async def check(self) -> None:
        since = self.disconnected_since
        if since is not None and not self.halted and time.time() - since > DISCONNECT_ALERT_S:
            await self._host.alert("disconnected", f"WhatsApp has been disconnected for "
                                   f"{int((time.time() - since) / 60)} minutes. "
                                   f"It keeps retrying by itself.")

    def label(self, chat: str) -> str:
        return self.chat_names.get(chat) or chat

    # ================================================================ WhatsApp events

    def _route_for(self, src) -> Optional[str]:
        chat = wa.jid_str(src.Chat)
        if chat in self._chat_set:
            return chat
        if chat in self.aliases:
            return self.aliases[chat]
        if not src.IsGroup:
            # 1:1 chats can be addressed by phone number or by hidden id (@lid).
            for alt in (src.SenderAlt, src.RecipientAlt):
                other = wa.jid_str(alt)
                if other in self._chat_set:
                    return other
        return None

    async def _on_message(self, ev) -> None:
        # No awaits before enqueue: handlers start in arrival order, so the
        # queue keeps WhatsApp's order.
        src = ev.Info.MessageSource
        chat = self._route_for(src)
        if self.debug:
            print(f"[debug] event {ev.Info.ID} chat={wa.jid_str(src.Chat)} routed={chat} "
                  f"fields={convert.set_fields(ev.Message)}")
        if chat is None:
            return
        if ev.Info.Pushname:
            self._host.store.remember_name(wa.jid_str(src.Sender), ev.Info.Pushname)
            self._host.store.remember_name(wa.jid_str(src.SenderAlt), ev.Info.Pushname)
        if convert.is_bookkeeping(ev):
            return  # e.g. the key handover that precedes a message's content
        self._host.enqueue(chat, ev.Info.ID, wa.serialize_event(ev), wa.payload_digest(ev))

    async def _on_connected(self, _ev) -> None:
        self.connected = True
        if self.disconnected_since is not None:
            print(f"WhatsApp reconnected after {int(time.time() - self.disconnected_since)}s.")
        self.disconnected_since = None
        if self._first_connect:
            self._first_connect = False
            print("WhatsApp connected.")
            await self._describe_chats()
            print("Listening.")
        self._host.wake()

    async def _on_disconnected(self, _ev) -> None:
        self.connected = False
        if self.disconnected_since is None and not self._host.stopping:
            self.disconnected_since = time.time()
            print("WhatsApp disconnected; reconnecting...")

    def _set_my_name(self, name: str) -> None:
        """Name used for the account's own messages (its WhatsApp profile name)."""
        name = (name or "").strip()
        if not name:
            return
        self.my_name = name
        if name != self.account.push_name and not self._host.dry_run:
            # Usually unknown at link time; keep the registry current.
            self.account.push_name = name
            accounts.upsert(self.account)

    async def _on_push_name_setting(self, ev) -> None:
        self._set_my_name(ev.Action.name)

    async def _on_offline_sync(self, ev) -> None:
        if ev.Count:
            print(f"WhatsApp delivered {ev.Count} event(s) from while the bridge was offline.")

    async def _on_keepalive_timeout(self, ev) -> None:
        if ev.ErrorCount in (3, 10, 30):
            print(f"WhatsApp keepalive failing ({ev.ErrorCount} in a row); the connection may be down.")

    async def _on_logged_out(self, ev) -> None:
        await self._halt("logged-out", "WhatsApp logged this device out "
                         f"({wa.connect_failure_reason(ev.Reason)}). It may have been removed under "
                         "Linked devices, or the phone was offline for over 14 days. "
                         "Re-link with wa-link, then restart the bridge.")

    async def _on_temp_ban(self, ev) -> None:
        hours = ev.Expire / 3600 if ev.Expire else 0
        until = f" for about {hours:.0f}h" if hours >= 1 else ""
        await self._halt("banned", f"WhatsApp temporarily banned the account{until} "
                         f"(reason: {wa.temp_ban_reason(ev.Code)}). The bridge has stopped "
                         "reading; restart it after the ban ends.")

    async def _on_outdated(self, _ev) -> None:
        await self._halt("outdated", "WhatsApp says this client version is outdated. "
                         "Update the neonize pin in requirements.txt, rebuild the image "
                         "and restart the bridge.")

    async def _on_unpaired(self) -> None:
        await self._halt("not-linked", "this WhatsApp account isn't linked any more. "
                         "Re-link it with wa-link, then restart the bridge.")

    async def _on_connect_failure(self, ev) -> None:
        reason = wa.connect_failure_reason(ev.Reason)
        print(f"WhatsApp refused the connection: {reason} {ev.Message}".rstrip())
        await self._host.alert("connect-failure", f"WhatsApp refused the connection ({reason}).")

    async def _on_stream_replaced(self, _ev) -> None:
        await self._host.alert("replaced", "another program connected with this WhatsApp session, "
                               "which disconnected the bridge. Restarting it in 60 seconds.")
        await self._host.sleep(60)
        self._host.request_stop(exit_code=1)

    async def _halt(self, key: str, text: str) -> None:
        if self.halted:
            return
        self.halted = text
        self.connected = False
        print(f"WhatsApp side stopped: {text}")
        await self._host.alert(key, text)
        if self.wa is not None:
            await wa.stop(self.wa)

    async def _describe_chats(self) -> None:
        try:
            me = await self.wa.get_me()
            for jid in (me.JID, me.LID):
                if wa.jid_str(jid):
                    self.my_jids.add(wa.jid_str(jid))
            self._set_my_name(await wa.own_profile_name(self.wa))
        except Exception:
            pass
        try:
            groups = {g.jid: g for g in await wa.joined_groups(self.wa)}
        except Exception as e:
            print(f"  (couldn't list WhatsApp groups to check the sources: {e})")
            groups = None
        for chat in self.chats:
            dests = self._host.dests_label(chat)
            if chat.endswith("@g.us"):
                group = groups.get(chat) if groups is not None else None
                if group is not None:
                    self.chat_names[chat] = group.name
                    print(f"  {group.name!r} ({chat}, {group.size} members) -> {dests}")
                elif groups is not None:
                    print(f"  WARNING: the account isn't in WhatsApp group {chat}; "
                          f"nothing will arrive from it.")
                    await self._host.alert(f"missing:{chat}", f"the linked account isn't a member "
                                           f"of WhatsApp group {chat}, so nothing arrives from it.")
            else:
                print(f"  chat {chat} -> {dests}")
                if chat.endswith("@s.whatsapp.net"):
                    lid = await wa.lid_for_phone(self.wa, chat)
                    if lid:
                        self.aliases[lid] = chat

    # ================================================================ messages

    def decode(self, item: InboxItem) -> Union[Message, Skip]:
        ev = wa.parse_message_event(item.raw)
        plan = convert.plan_message(ev, self._name_for)
        if isinstance(plan, Skip):
            return plan
        reply = None
        if plan.reply is not None:
            reply = Quote(plan.reply.msg_id, self._name_for(plan.reply.sender) or "",
                          plan.reply.preview)
        return Message(id=ev.Info.ID, kind=plan.kind, label=plan.label,
                       sender_name=self._sender_name(ev), body=plan.body, media=plan.media,
                       poll=plan.poll, reply=reply, edit_of=plan.edit_of)

    def _name_for(self, jid: str) -> Optional[str]:
        if not jid:
            return None
        return self._host.store.name_for(jid) or (self.my_name if jid in self.my_jids else None)

    def _sender_name(self, ev) -> str:
        info = ev.Info
        name = (info.Pushname or "").strip()
        if not name:
            src = info.MessageSource
            name = (self._name_for(wa.jid_str(src.Sender))
                    or self._name_for(wa.jid_str(src.SenderAlt)) or "")
        if not name and info.MessageSource.IsFromMe:
            name = self.my_name
        return name or "Unknown"

    async def download(self, msg: Message, folder: Path) -> Path:
        spec: convert.Media = msg.media
        path = folder / media_filename(spec)
        last = None
        for attempt in range(1, DOWNLOAD_TRIES + 1):
            if not self.connected or self.wa is None:
                raise NotConnected()
            try:
                await wa.download(self.wa, spec.field, spec.proto, path)
                if path.exists() and path.stat().st_size > 0:
                    return path
                last = "empty file"
            except Exception as e:
                last = str(e) or repr(e)
            if attempt < DOWNLOAD_TRIES:
                await self._host.sleep(5)
        if not self.connected:
            raise NotConnected()
        raise MediaUnavailable(last)
