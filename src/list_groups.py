"""Print every group / channel the selected user is in,
with chat-id and access_hash so they can be plugged into `forward`.
"""
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat

from . import config, users


async def list_groups(short_id: str) -> None:
    config.validate(require_bot=False)
    record = users.get_user(short_id)

    client = TelegramClient(
        str(record.session_base), config.api_id_int(), config.TG_API_HASH
    )
    await client.connect()

    if not await client.is_user_authorized():
        print(f"User {short_id} not authorized. Run `login` again for that account.")
        await client.disconnect()
        return

    print(f"Listing groups visible to {record.display_name} (short_id={short_id})")
    header = f"{'TYPE':<11}{'CHAT_ID':<18}{'ACCESS_HASH':<22}TITLE"
    print(header)
    print("-" * len(header) * 2)

    found = 0
    async for dialog in client.iter_dialogs():
        ent = dialog.entity

        if isinstance(ent, Channel):
            if ent.broadcast:
                kind = "channel"
            elif getattr(ent, "forum", False):
                # Forum-enabled supergroup ("community") — has topics inside.
                kind = "community"
            else:
                kind = "supergroup"
            access_hash = ent.access_hash
            display_id = f"-100{ent.id}"
        elif isinstance(ent, Chat):
            # Basic groups have no access_hash. Print 0 — that's the sentinel
            # this tool uses to mean "this is a basic group, build InputPeerChat".
            kind = "group"
            access_hash = 0
            display_id = f"-{ent.id}"
        else:
            # Skip private chats / users
            continue

        print(f"{kind:<11}{display_id:<18}{str(access_hash):<22}{dialog.name}")
        found += 1

    if found == 0:
        print("(no groups or channels found)")

    await client.disconnect()


async def list_topics(short_id: str, parent_id_str: str) -> None:
    """Print every topic inside a forum-enabled supergroup ("community").

    Topics in Telegram aren't separate chats — they live inside the parent
    supergroup as message threads, identified by the id of the topic's first
    message. That id is what `forward -tid` takes.
    """
    # Telethon moved this RPC between modules across versions: older builds
    # have it under channels (channel: InputChannel), newer builds expose it
    # under messages (peer: InputPeer). Try channels first, fall back.
    GetForumTopicsRequest = None
    request_uses_peer = False
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest  # type: ignore
    except ImportError:
        try:
            from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore
            request_uses_peer = True
        except ImportError as e:
            raise SystemExit(
                "This Telethon build does not export GetForumTopicsRequest. "
                f"Upgrade telethon (`pip install -U telethon`). ({e})"
            )

    from .forwarder import parse_chat_id

    config.validate(require_bot=False)
    record = users.get_user(short_id)

    client = TelegramClient(
        str(record.session_base), config.api_id_int(), config.TG_API_HASH
    )
    await client.connect()

    if not await client.is_user_authorized():
        print(f"User {short_id} not authorized. Run `login` again for that account.")
        await client.disconnect()
        return

    parent_id = parse_chat_id(parent_id_str)
    marked = int(f"-100{parent_id}")
    try:
        entity = await client.get_entity(marked)
    except Exception:
        # Cold session cache — prime it once and retry.
        print("  priming session cache (first lookup on this session)...")
        async for _ in client.iter_dialogs():
            pass
        try:
            entity = await client.get_entity(marked)
        except Exception as e:
            print(f"Could not resolve parent supergroup -100{parent_id}: {e}")
            await client.disconnect()
            return

    if not isinstance(entity, Channel) or not getattr(entity, "forum", False):
        print(f"{entity.title!r} is not a forum-enabled supergroup (no topics).")
        await client.disconnect()
        return

    common_kwargs = dict(offset_date=None, offset_id=0, offset_topic=0, limit=100)
    if request_uses_peer:
        request = GetForumTopicsRequest(peer=entity, **common_kwargs)
    else:
        request = GetForumTopicsRequest(channel=entity, **common_kwargs)
    result = await client(request)

    print(f"Topics in {entity.title!r} (parent_chat_id=-100{parent_id})")
    header = f"{'TOPIC_ID':<12}{'CLOSED':<8}TITLE"
    print(header)
    print("-" * (len(header) * 2))

    if not result.topics:
        print("(no topics found)")
    for topic in result.topics:
        # `id` here is the topic's top-message id — what `forward -tid` wants.
        closed = "yes" if getattr(topic, "closed", False) else ""
        print(f"{topic.id:<12}{closed:<8}{topic.title}")

    await client.disconnect()


async def clone_topics(short_id: str, src_id_str: str, dst_id_str: str) -> None:
    """Create matching topics in `dst` for every topic that exists in `src`.

    Both `src` and `dst` must be forum-enabled supergroups, and the user
    account selected by `-u` must be an admin in `dst` with permission to
    manage topics. Existing topics with the same title in `dst` are skipped.
    """
    GetForumTopicsRequest = None
    request_uses_peer = False
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest  # type: ignore
    except ImportError:
        try:
            from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore
            request_uses_peer = True
        except ImportError as e:
            raise SystemExit(
                "This Telethon build does not export GetForumTopicsRequest. "
                f"Upgrade telethon. ({e})"
            )

    from .forwarder import parse_chat_id
    import secrets

    # Same module-shuffle dance as GetForumTopicsRequest — newer Telethon
    # builds expose this under `messages`, older ones under `channels`.
    # Both schema variants exist in the upstream MTProto definition.
    CreateForumTopicRequest = None
    create_uses_peer = False
    try:
        from telethon.tl.functions.channels import CreateForumTopicRequest  # type: ignore
    except ImportError:
        try:
            from telethon.tl.functions.messages import CreateForumTopicRequest  # type: ignore
            create_uses_peer = True
        except ImportError as e:
            raise SystemExit(
                "This Telethon build does not export CreateForumTopicRequest. "
                f"Upgrade telethon (`pip install -U telethon`). ({e})"
            )

    config.validate(require_bot=False)
    record = users.get_user(short_id)

    client = TelegramClient(
        str(record.session_base), config.api_id_int(), config.TG_API_HASH
    )
    await client.connect()
    if not await client.is_user_authorized():
        print(f"User {short_id} not authorized.")
        await client.disconnect()
        return

    src_marked = int(f"-100{parse_chat_id(src_id_str)}")
    dst_marked = int(f"-100{parse_chat_id(dst_id_str)}")

    primed = False

    async def _resolve(marked):
        nonlocal primed
        try:
            return await client.get_entity(marked)
        except Exception:
            if primed:
                raise
            print("  priming session cache (first lookup on this session)...")
            async for _ in client.iter_dialogs():
                pass
            primed = True
            return await client.get_entity(marked)

    try:
        src = await _resolve(src_marked)
        dst = await _resolve(dst_marked)
    except Exception as e:
        print(f"Could not resolve communities: {e}")
        await client.disconnect()
        return

    for ent, label in ((src, "source"), (dst, "destination")):
        if not (isinstance(ent, Channel) and getattr(ent, "forum", False)):
            print(f"{label} {ent.title!r} is not a forum-enabled supergroup.")
            await client.disconnect()
            return

    common_kwargs = dict(offset_date=None, offset_id=0, offset_topic=0, limit=100)
    if request_uses_peer:
        src_topics = await client(GetForumTopicsRequest(peer=src, **common_kwargs))
        dst_topics = await client(GetForumTopicsRequest(peer=dst, **common_kwargs))
    else:
        src_topics = await client(GetForumTopicsRequest(channel=src, **common_kwargs))
        dst_topics = await client(GetForumTopicsRequest(channel=dst, **common_kwargs))

    existing_titles = {t.title for t in dst_topics.topics}
    print(f"Cloning topics from {src.title!r} -> {dst.title!r}")
    print(f"  destination already has: {sorted(existing_titles)}")

    created = 0
    skipped = 0
    for topic in src_topics.topics:
        if topic.id == 1:
            # Topic id 1 is the General topic — every forum has one
            # automatically and you can't re-create it.
            print("  skipping General (built-in)")
            continue
        if topic.title in existing_titles:
            print(f"  skipping {topic.title!r} (already exists in dst)")
            skipped += 1
            continue
        try:
            create_kwargs = dict(
                title=topic.title,
                icon_color=getattr(topic, "icon_color", None),
                icon_emoji_id=getattr(topic, "icon_emoji_id", None),
                random_id=secrets.randbits(63),
            )
            if create_uses_peer:
                create_kwargs["peer"] = dst
            else:
                create_kwargs["channel"] = dst
            await client(CreateForumTopicRequest(**create_kwargs))
            print(f"  created {topic.title!r}")
            created += 1
        except Exception as e:
            print(f"  failed to create {topic.title!r}: {e}")

    print(f"Done. Created {created}, skipped {skipped}.")
    print("Run `list-topics` on the destination to see the new TOPIC_IDs.")
    await client.disconnect()
