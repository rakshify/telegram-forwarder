"""JSON config file support for `forward`.

Config schema
-------------
{
  "user": "abc123",                       # optional; otherwise pass -u

  "pairs": [
    {
      "source": "-1001969809629",
      "source_hash": 1234567890123456789,
      "dest":   "-1005555555555",
      "dest_hash": 0,
      "topic": 17,                        # optional, source topic; default 0
      "dest_topic": 5                     # optional, destination topic; default 0
    },
    ...
  ],

  # OR — instead of (or in addition to) `pairs`, an "auto" block that takes
  # all topics of a source community and forwards each into the destination
  # community's same-named topic. Useful for mirroring a community.
  "auto": [
    {
      "source": "-1001969809629",
      "source_hash": 1234567890123456789,
      "dest":   "-1005555555555",
      "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"],   # optional whitelist by title
      "exclude": ["General"]                         # optional blacklist by title
    }
  ]
}

Auto blocks are resolved at load time using the user's session — they query
both communities for their topic lists and match topics by title.
"""
import json
from pathlib import Path
from typing import List, Optional

from telethon import TelegramClient
from telethon.tl.types import Channel

from . import config, users
from .forwarder import ChatPair, parse_chat_id


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


async def load_config(
    path: Path, fallback_short_id: Optional[str] = None
) -> tuple[str, List[ChatPair]]:
    """Read a config file and return (short_id, [ChatPair, ...]).

    `fallback_short_id` is used when the file does not specify a `user`.
    """
    with path.open() as f:
        cfg = json.load(f)

    short_id = cfg.get("user") or fallback_short_id
    if not short_id:
        raise SystemExit(
            f"{path}: no 'user' specified in config and no -u given on the CLI."
        )

    pairs: List[ChatPair] = []

    # 1) Explicit per-pair entries
    for i, entry in enumerate(cfg.get("pairs") or []):
        try:
            pairs.append(ChatPair(
                source_id=parse_chat_id(str(entry["source"])),
                source_hash=_normalize_int(entry["source_hash"]),
                dest_id=parse_chat_id(str(entry["dest"])),
                dest_hash=_normalize_int(entry["dest_hash"]),
                topic_id=_normalize_int(entry.get("topic", 0)),
                dest_topic_id=_normalize_int(entry.get("dest_topic", 0)),
            ))
        except KeyError as e:
            raise SystemExit(f"{path}: pairs[{i}] missing key {e}")

    # 2) Auto blocks — expand to one ChatPair per topic by matching titles
    auto_blocks = cfg.get("auto") or []
    if auto_blocks:
        pairs.extend(await _expand_auto_blocks(short_id, auto_blocks, path))

    if not pairs:
        raise SystemExit(f"{path}: no pairs defined (need 'pairs' or 'auto').")

    return short_id, pairs


async def _expand_auto_blocks(
    short_id: str, blocks: list, path: Path
) -> List[ChatPair]:
    """Resolve each auto block into one ChatPair per matching topic title."""
    config.validate(require_bot=False)
    record = users.get_user(short_id)

    GetForumTopicsRequest, uses_peer_kw = _import_get_topics_request()

    client = TelegramClient(
        str(record.session_base), config.api_id_int(), config.TG_API_HASH
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(f"User {short_id} not authorized — run `login` first.")

    expanded: List[ChatPair] = []
    common_kwargs = dict(offset_date=None, offset_id=0, offset_topic=0, limit=100)

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

            src_marked = int(f"-100{parse_chat_id(src_id_raw)}")
            dst_marked = int(f"-100{parse_chat_id(dst_id_raw)}")

            try:
                src_ent = await client.get_entity(src_marked)
                dst_ent = await client.get_entity(dst_marked)
            except Exception as e:
                raise SystemExit(f"auto[{i}]: could not resolve communities: {e}")

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

                expanded.append(ChatPair(
                    source_id=parse_chat_id(src_id_raw),
                    source_hash=src_hash,
                    dest_id=parse_chat_id(dst_id_raw),
                    dest_hash=dst_hash,
                    topic_id=topic.id,
                    dest_topic_id=dest_topic_id,
                ))
                print(f"  pair: topic {topic.id} ({topic.title!r}) "
                      f"-> dest topic {dest_topic_id}")
    finally:
        await client.disconnect()

    return expanded
