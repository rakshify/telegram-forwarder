"""Everything that touches neonize (the WhatsApp client library) lives here.

neonize wraps whatsmeow (Go) and its Python API changes between releases
(requirements.txt pins it exactly). The rest of the WhatsApp adapter
(src/adapters/whatsapp) only uses this module, so upgrading neonize means re-checking this file and re-running the
tests, nothing else.

How neonize behaves, as relied on below (checked against 0.12.4):

* `NewAClient(<sqlite path>)` keeps the session in that SQLite file; with
  one account per file, connect() resumes it or starts pairing.
* Go delivers events in order; neonize schedules each handler coroutine on
  the event loop that called connect(), without waiting for it.
* While unpaired, connect() emits QR events. Calling PairPhone() then
  returns an 8-character code for "Link with phone number instead".
  WhatsApp's pairing window is about 2.5 minutes, after which the QR
  channel reports "timeout" and the socket closes.
* Every FFI call runs in a worker thread (asyncio.to_thread), so media
  downloads don't block the event loop that Telethon also uses.
"""
import asyncio
import ctypes
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

# neonize configures the root logger at import (logging.basicConfig at
# INFO); quiet_logs() turns it back down.
import neonize.utils.ffmpeg as _ffmpeg
from neonize.aioze import events as _events
from neonize.aioze.client import ClientFactory, NewAClient
from neonize.proto import Neonize_pb2
from neonize.proto.Neonize_pb2 import JID
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message as WAMessage
from neonize.utils.enum import ClientName, ClientType
from neonize.utils.jid import Jid2String, JIDToNonAD

MessageEv = _events.MessageEv
ConnectedEv = _events.ConnectedEv
DisconnectedEv = _events.DisconnectedEv
LoggedOutEv = _events.LoggedOutEv
TemporaryBanEv = _events.TemporaryBanEv
ClientOutdatedEv = _events.ClientOutdatedEv
StreamReplacedEv = _events.StreamReplacedEv
ConnectFailureEv = _events.ConnectFailureEv
KeepAliveTimeoutEv = _events.KeepAliveTimeoutEv
OfflineSyncCompletedEv = _events.OfflineSyncCompletedEv
PairStatusEv = _events.PairStatusEv
PushNameSettingEv = _events.PushNameSettingEv  # .Action.name: the account's own profile name

# How the bridge appears on the phone under Settings > Linked devices.
DEVICE_NAME = "Telegram bridge"


def quiet_logs(debug: bool = False) -> None:
    """Keep neonize/whatsmeow/Telethon at warnings; the bridge prints its own
    one-line status messages, like the Telegram forwarder."""
    level = logging.DEBUG if debug else logging.WARNING
    logging.getLogger().setLevel(level)
    for name in ("neonize", "whatsmeow", "Whatsmeow", "telethon"):
        logging.getLogger(name).setLevel(level)
    # whatsmeow's own (Go-side) level is read from this logger's level at connect().
    logging.getLogger("neonize.utils.log").setLevel(level)
    # neonize warns at connect() when ffmpeg is missing. ffmpeg is only used
    # to *send* media to WhatsApp, which the bridge never does.
    _ffmpeg._ffmpeg_checked = True


def _props() -> DeviceProps:
    return DeviceProps(os=DEVICE_NAME, platformType=DeviceProps.SAFARI)


def make_client(db_path: Path) -> NewAClient:
    return NewAClient(str(db_path), props=_props())


@dataclass
class StoredDevice:
    jid: str
    push_name: str


def stored_device(db_path: Path) -> Optional[StoredDevice]:
    """The linked device stored in `db_path`, or None (never creates the file).

    Reads whatsmeow's device table directly: neonize's own helper splits
    the stored profile name on commas and crashes on a name like "A, B".
    """
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM whatsmeow_device LIMIT 1").fetchone()
        finally:
            con.close()
        if row is None:
            return None
        keys = row.keys()
        return StoredDevice(jid=row["jid"], push_name=row["push_name"] if "push_name" in keys else "")
    except sqlite3.Error:
        # Table missing (empty or half-created file) or a schema change.
        try:
            devices = ClientFactory.get_all_devices_from_db(str(path))
        except Exception:
            return None
        if not devices:
            return None
        return StoredDevice(jid=jid_str(devices[0].JID), push_name=devices[0].PushName)


# ------------------------------------------------------------------ ids

def jid_str(jid) -> str:
    """JID proto -> 'user@server' without the device part ('' if unset)."""
    if jid is None or (not jid.User and not jid.Server):
        return ""
    return Jid2String(JIDToNonAD(jid))


def jid_from_str(s: str) -> JID:
    user, _, server = s.partition("@")
    return JID(User=user, Server=server, RawAgent=0, Device=0, Integrator=0, IsEmpty=False)


def serialize_event(ev) -> bytes:
    # The event proto marks most fields "required"; the lenient form never
    # raises if a future neonize leaves one unset.
    return ev.SerializePartialToString()


def payload_digest(ev) -> str:
    """Short fingerprint of an event's message content (for de-duplication)."""
    import hashlib
    return hashlib.sha1(ev.Message.SerializePartialToString(deterministic=True)).hexdigest()[:20]


def parse_message_event(raw: bytes):
    ev = MessageEv()
    ev.MergeFromString(raw)
    return ev


def connect_failure_reason(value: int) -> str:
    try:
        return Neonize_pb2.ConnectFailureReason.Name(value)
    except ValueError:
        return str(value)


def temp_ban_reason(value: int) -> str:
    try:
        return TemporaryBanEv.TempBanReason.Name(value)
    except ValueError:
        return str(value)


# ------------------------------------------------------------------ handlers

def on(client: NewAClient, event_type, handler) -> None:
    """Register `async handler(event)` for an event type."""
    async def _call(_client, event):
        await handler(event)
    client.event(event_type)(_call)


def on_qr(client: NewAClient, handler) -> None:
    """Register `async handler()` for "unpaired, showing a QR code"."""
    async def _call(_client, _codes):
        await handler()
    client.qr(_call)


async def download(client: NewAClient, field: str, media_proto, path: Path) -> None:
    """Download and decrypt one media sub-message straight to `path`."""
    msg = WAMessage()
    getattr(msg, field).CopyFrom(media_proto)
    await client.download_any(msg, path=str(path))


async def lid_for_phone(client: NewAClient, phone_jid: str) -> str:
    """The hidden-number id (…@lid) WhatsApp may use for a 1:1 chat."""
    try:
        return jid_str(await client.get_lid_from_pn(jid_from_str(phone_jid)))
    except Exception:
        return ""


async def own_profile_name(client: NewAClient) -> str:
    """The linked account's own WhatsApp profile name.

    It arrives with the account's settings ("app state"), which can be
    missing when that first sync was cut short, e.g. by disconnecting right
    after linking. If so, fetch the small collection that holds it.
    """
    me = await client.get_me()
    if me.PushName:
        return me.PushName
    try:
        await client.fetch_app_state("critical_block", full_sync=True)
    except Exception:
        return ""
    return (await client.get_me()).PushName


async def stop(client: NewAClient) -> None:
    try:
        await client.stop()
    except Exception:
        pass
    task = getattr(client, "connect_task", None)
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, 10)
        except BaseException:
            pass


# ------------------------------------------------------------------ one-off sessions

async def connect_ready(db_path: Path, timeout: float = 90) -> NewAClient:
    """Connect a linked session and wait until it's usable (for one-off commands)."""
    client = make_client(db_path)
    ready = asyncio.Event()
    problem = {}

    async def _connected(_ev):
        ready.set()

    async def _logged_out(ev):
        problem["msg"] = f"WhatsApp has logged this device out ({connect_failure_reason(ev.Reason)})."
        ready.set()

    async def _unpaired():
        problem["msg"] = "This WhatsApp account isn't linked (run wa-link)."
        ready.set()

    on(client, ConnectedEv, _connected)
    on(client, LoggedOutEv, _logged_out)
    on_qr(client, _unpaired)
    await client.connect()
    try:
        await asyncio.wait_for(ready.wait(), timeout)
    except asyncio.TimeoutError:
        problem["msg"] = f"Couldn't connect to WhatsApp within {int(timeout)}s."
    if problem:
        await stop(client)
        raise SystemExit(problem["msg"])
    return client


@dataclass
class GroupRow:
    jid: str
    name: str
    size: int
    kind: str      # group | community | announcements
    parent: str    # parent community id ('' if none)


async def joined_groups(client: NewAClient) -> List[GroupRow]:
    rows = []
    for g in await client.get_joined_groups():
        if g.GroupParent.IsParent:
            kind = "community"
        elif g.GroupIsDefaultSub.IsDefaultSubGroup:
            kind = "announcements"
        else:
            kind = "group"
        rows.append(GroupRow(
            jid=jid_str(g.JID),
            name=g.GroupName.Name,
            size=len(g.Participants),
            kind=kind,
            parent=jid_str(g.GroupLinkedParent.LinkedParentJID),
        ))
    return rows


# ------------------------------------------------------------------ pairing

class _LinkClient(NewAClient):
    """NewAClient that also reports pairing status ("success", "timeout", ...).

    neonize passes these to a private method that does nothing. Overriding
    it by its mangled name is the only way to learn that a code expired.
    """
    status_queue: Optional[asyncio.Queue] = None

    def _NewAClient__onLoginStatus(self, uuid, status):  # called on a Go thread
        queue, loop = self.status_queue, self.loop
        if queue is None or loop is None:
            return
        text = ctypes.string_at(status).decode(errors="replace") if status else ""
        try:
            loop.call_soon_threadsafe(queue.put_nowait, text)
        except RuntimeError:
            pass  # loop already closed


async def link(
    db_path: Path,
    phone: str,
    on_code: Callable[[str, int], None],
    attempts: int = 3,
):
    """Link the WhatsApp account of `phone` (digits incl. country code).

    Calls on_code(code, attempt) with each pairing code. When a code
    expires unused, a new one is made, up to `attempts` codes in total.
    Returns the linked device (neonize Device: JID, PushName, ...).
    """
    for attempt in range(1, attempts + 1):
        client = _LinkClient(str(db_path), props=_props())
        client.status_queue = asyncio.Queue()
        state = {"requested": False, "error": None}
        connected = asyncio.Event()

        async def _unpaired(c=client, st=state, n=attempt):
            if st["requested"]:
                return  # QR codes keep rotating; one pairing code per attempt
            st["requested"] = True
            try:
                code = await c.PairPhone(
                    phone, True, client_name=ClientName.LINUX, client_type=ClientType.FIREFOX,
                )
            except Exception as e:
                st["error"] = f"WhatsApp refused to make a pairing code: {e}"
                c.status_queue.put_nowait("error")
                return
            on_code(code, n)

        async def _pair_status(ev, st=state):
            if ev.Status != PairStatusEv.SUCCESS:
                st["error"] = ev.Error or "pairing failed"

        async def _connected(_ev, ev_=connected):
            ev_.set()

        on_qr(client, _unpaired)
        on(client, PairStatusEv, _pair_status)
        on(client, ConnectedEv, _connected)
        await client.connect()

        try:
            status = await asyncio.wait_for(client.status_queue.get(), 300)
        except asyncio.TimeoutError:
            status = "timeout"

        if status == "success":
            try:
                await asyncio.wait_for(connected.wait(), 90)
            except asyncio.TimeoutError:
                await stop(client)
                raise SystemExit("Paired, but the first connection didn't complete. Try wa-link again.")
            if state["error"]:
                await stop(client)
                raise SystemExit(f"Pairing failed: {state['error']}")
            me = await client.get_me()
            # Give WhatsApp a moment to finish the post-pairing key exchange
            # before disconnecting.
            await asyncio.sleep(8)
            await stop(client)
            return me

        await stop(client)
        if status == "timeout" and not state["error"]:
            if attempt < attempts:
                print("That code expired before it was entered. Making a new one...", flush=True)
            continue
        raise SystemExit(state["error"] or f"Pairing stopped: {status}")

    raise SystemExit(f"No code was entered in time ({attempts} codes). Run wa-link again when ready.")
