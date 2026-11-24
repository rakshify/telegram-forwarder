"""What a config says about Telegram sources (the format itself: src/adapters/routes.py).

A Telegram pair names its source chat like this:

    "source": "-1004200000003",          # chat id
    "source_hash": 1234567890123456789,  # its access hash from list-groups (0 for basic groups)
    "topic": 17                          # optional: only this forum topic (default 0, the whole chat)

Instead of (or as well as) `pairs`, a Telegram config can have `auto` blocks
that take every topic of a source community and forward each into the
destination community's same-named topic:

  "auto": [
    {
      "source": "-1004200000003",
      "source_hash": 1234567890123456789,
      "dest":   "-1005555555555",
      "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"],   # optional whitelist by title
      "exclude": ["General"]                         # optional blacklist by title
    }
  ]

Auto blocks are resolved at load time using the user's session — they query
both communities for their topic lists and match topics by title.

The `forward` command's flags (-gid/-gh/-mid/-mh/-tid/-mtid) describe the
same thing without a file (from_cli).
"""
from pathlib import Path
from typing import List, Optional, Tuple

from telethon.tl.types import Channel

from ... import config, users
from ...core.model import TELEGRAM, Dest, Routes
from .peers import parse_chat_id
from .source import Chat


def _import_get_topics_request():
    """Return (cls, uses_peer_kw) for whichever Telethon variant is installed."""
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest  # type: ignore
        return GetForumTopicsRequest, False
    except ImportError:
        from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore
        return GetForumTopicsRequest, True


def _normalize_int(v) -> int:
    """Accept either an int or a string for ids and hashes."""
    if isinstance(v, str):
        return int(v)
    return int(v)


def parse_source(entry: dict) -> Tuple[str, Chat]:
    """A pair's source chat: (its key in the pipeline, the Chat the source reads)."""
    chat = Chat(parse_chat_id(str(entry["source"])), _normalize_int(entry["source_hash"]),
                _normalize_int(entry.get("topic", 0)))
    return chat.key, chat


def from_cli(user: str, gids: List[str], ghs: List[str], mids: List[str], mhs: List[str],
             tids: Optional[List[str]], mtids: Optional[List[str]], attribution: bool) -> Routes:
    """The pairs given as `forward` flags (-gid/-gh/-mid/-mh/-tid/-mtid), 1:1 by position."""
    tids = tids if tids is not None else ["0"] * len(gids)
    mtids = mtids if mtids is not None else ["0"] * len(gids)

    n = len(gids)
    if not (len(ghs) == len(mids) == len(mhs) == n):
        raise SystemExit(
            "Length mismatch: -gid, -gh, -mid, -mh must all have the same number of values "
            f"(got {len(gids)}, {len(ghs)}, {len(mids)}, {len(mhs)})."
        )
    if len(tids) != n:
        raise SystemExit(
            f"-tid must have the same number of values as -gid "
            f"(got {len(tids)} vs {n})."
        )
    if len(mtids) != n:
        raise SystemExit(
            f"-mtid must have the same number of values as -mid "
            f"(got {len(mtids)} vs {n})."
        )
    if n == 0:
        raise SystemExit("At least one source/destination pair is required.")

    routes = Routes(platform=TELEGRAM, account=user)
    for gid, gh, mid, mh, tid, mtid in zip(gids, ghs, mids, mhs, tids, mtids):
        chat = Chat(parse_chat_id(gid), int(gh), int(tid))
        routes.add(chat.key, chat, Dest(TELEGRAM, str(parse_chat_id(mid)), hash=int(mh), topic=int(mtid)),
                   attribution)
    return routes


async def expand_auto(routes: Routes, blocks: list, path: Path, default_attribution: bool = False) -> None:
    """Resolve each auto block into one pair per matching topic title."""
    config.validate(require_bot=False)
    record = users.get_user(routes.account)

    GetForumTopicsRequest, uses_peer_kw = _import_get_topics_request()

    client = config.make_client(record.session_base)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(f"User {routes.account} not authorized — run `login` first.")

    common_kwargs = dict(offset_date=None, offset_id=0, offset_topic=0, limit=100)

    # Telethon caches entity → access_hash mappings in the session SQLite. If a
    # community we're targeting was never opened on this session before,
    # get_entity(-100<id>) fails with "Could not find the input entity".
    # iter_dialogs() walks every chat the user is in and populates that cache
    # as a side effect. We prime once, lazily, only if needed.
    primed = False

    async def _resolve(marked_id: int):
        nonlocal primed
        try:
            return await client.get_entity(marked_id)
        except (ValueError, Exception):
            if primed:
                raise
            print(f"  priming session cache (first lookup of "
                  f"{marked_id} on this session)...")
            async for _ in client.iter_dialogs():
                pass
            primed = True
            return await client.get_entity(marked_id)

    try:
        for i, block in enumerate(blocks):
            try:
                src_id_raw = str(block["source"])
                dst_id_raw = str(block["dest"])
                src_hash = _normalize_int(block["source_hash"])
                dst_hash = _normalize_int(block["dest_hash"])
            except KeyError as e:
                raise SystemExit(f"{path}: auto[{i}] missing key {e}")

            include = set(block.get("include") or [])
            exclude = set(block.get("exclude") or [])
            block_attribution = bool(block.get("attribution", default_attribution))

            src_marked = int(f"-100{parse_chat_id(src_id_raw)}")
            dst_marked = int(f"-100{parse_chat_id(dst_id_raw)}")

            try:
                src_ent = await _resolve(src_marked)
                dst_ent = await _resolve(dst_marked)
            except Exception as e:
                raise SystemExit(
                    f"auto[{i}]: could not resolve communities even after priming "
                    f"the session cache: {e}\n"
                    f"Make sure the user account is a member of both communities."
                )

            for ent, label in ((src_ent, "source"), (dst_ent, "dest")):
                if not (isinstance(ent, Channel) and getattr(ent, "forum", False)):
                    raise SystemExit(
                        f"auto[{i}]: {label} {ent.title!r} is not a forum-enabled supergroup."
                    )

            if uses_peer_kw:
                src_topics = await client(GetForumTopicsRequest(peer=src_ent, **common_kwargs))
                dst_topics = await client(GetForumTopicsRequest(peer=dst_ent, **common_kwargs))
            else:
                src_topics = await client(GetForumTopicsRequest(channel=src_ent, **common_kwargs))
                dst_topics = await client(GetForumTopicsRequest(channel=dst_ent, **common_kwargs))

            dst_by_title = {t.title: t.id for t in dst_topics.topics}

            print(f"auto[{i}]: {src_ent.title!r} -> {dst_ent.title!r}")
            for topic in src_topics.topics:
                if include and topic.title not in include:
                    continue
                if topic.title in exclude:
                    continue

                dest_topic_id = dst_by_title.get(topic.title)
                if dest_topic_id is None:
                    print(f"  skipping {topic.title!r}: no matching topic in destination")
                    continue

                chat = Chat(parse_chat_id(src_id_raw), src_hash, topic.id)
                routes.add(chat.key, chat,
                           Dest(TELEGRAM, str(parse_chat_id(dst_id_raw)), hash=dst_hash,
                                topic=dest_topic_id),
                           block_attribution)
                print(f"  pair: topic {topic.id} ({topic.title!r}) "
                      f"-> dest topic {dest_topic_id}")
    finally:
        await client.disconnect()
