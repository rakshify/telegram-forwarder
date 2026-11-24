"""Telegram chat ids and turning them into peers a client can use.

Moved here unchanged from the old src/forwarder.py.
"""
from telethon import TelegramClient
from telethon.tl.types import InputPeerChannel


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


async def resolve_peer(
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

    Raises SystemExit with an explanation when the chat can't be resolved.
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
