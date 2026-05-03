"""User-session authentication flows.

`login`  : interactive — prompts for phone number, OTP, and 2FA password if needed.
`logout` : revokes the user session server-side and removes the local session files.
"""
import sys
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from . import config


async def login() -> None:
    """Interactive login. Stores a session file at config.USER_SESSION_PATH.

    Run this against a TTY (locally, or via `docker compose run -it forwarder login`).
    """
    config.validate(require_bot=False)
    print(f"User session will be saved to: {config.USER_SESSION_PATH}.session")

    client = TelegramClient(
        str(config.USER_SESSION_PATH),
        config.api_id_int(),
        config.TG_API_HASH,
    )
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        uname = f"@{me.username}" if me.username else "(no username)"
        print(f"Already logged in as {me.first_name} {uname}. "
              "Run `logout` first if you want to switch accounts.")
        await client.disconnect()
        return

    if not sys.stdin.isatty():
        print(
            "Login requires a TTY for the OTP / 2FA prompts.\n"
            "Re-run with: docker compose run --rm forwarder login",
            file=sys.stderr,
        )
        await client.disconnect()
        raise SystemExit(1)

    phone = input("Phone number with country code (e.g. +14155551234): ").strip()
    sent = await client.send_code_request(phone)
    code = input("OTP code received on Telegram: ").strip()

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = getpass("Two-factor authentication password: ")
        await client.sign_in(password=password)

    me = await client.get_me()
    uname = f"@{me.username}" if me.username else "(no username)"
    print(f"Logged in as {me.first_name} {uname}.")
    await client.disconnect()


async def logout() -> None:
    """Revoke the user session on Telegram's servers and delete local session files."""
    config.validate(require_bot=False)

    client = TelegramClient(
        str(config.USER_SESSION_PATH),
        config.api_id_int(),
        config.TG_API_HASH,
    )
    await client.connect()

    if await client.is_user_authorized():
        try:
            await client.log_out()
            print("Server-side session revoked.")
        except Exception as e:
            print(f"Server-side log_out failed (continuing with local cleanup): {e}")
    else:
        print("No active server-side session.")

    await client.disconnect()

    # Delete local session artifacts
    removed = []
    for suffix in (".session", ".session-journal"):
        path = config.USER_SESSION_PATH.with_suffix(suffix)
        if path.exists():
            path.unlink()
            removed.append(path.name)

    if removed:
        print(f"Removed local file(s): {', '.join(removed)}")
    else:
        print("No local session file to remove.")
