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
            kind = "channel" if ent.broadcast else "supergroup"
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
