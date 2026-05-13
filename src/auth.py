"""Multi-user authentication: login, logout, list-users.

Every login produces a UserRecord stored in users.json and a session file
named `user_<short_id>.session`. The short_id is deterministic (hash of the
Telegram user id), so re-logging into the same account refreshes that
account's session in place rather than creating a duplicate.
"""
import secrets
import sys
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from . import config, users


async def login() -> None:
    """Interactive login. Registers the resulting account in users.json under a
    deterministic short_id derived from its Telegram user id.
    """
    config.validate(require_bot=False)

    if not sys.stdin.isatty():
        raise SystemExit(
            "Login requires a TTY for OTP / 2FA prompts.\n"
            "Use: docker compose run --rm forwarder login"
        )

    # Login under a temp session — we can't pick the final filename until we
    # know the user's id (only available via get_me() after sign_in).
    tmp_base = config.SESSION_DIR / f"_tmp_login_{secrets.token_hex(4)}"
    client = config.make_client(tmp_base)
    await client.connect()

    try:
        phone = input("Phone number with country code (e.g. +14155551234): ").strip()
        sent = await client.send_code_request(phone)
        code = input("OTP code received on Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass("Two-factor authentication password: ")
            await client.sign_in(password=password)

        me = await client.get_me()
    except BaseException:
        # On any failure, drop the temp session so we don't litter the volume
        await client.disconnect()
        users.cleanup_session_files(tmp_base)
        raise

    short_id = users.make_short_id(me.id)
    record = users.UserRecord(
        short_id=short_id,
        user_id=me.id,
        first_name=me.first_name,
        last_name=me.last_name,
        username=me.username,
        phone=me.phone,
    )

    await client.disconnect()

    # Move temp session files to the per-user location, replacing any prior
    # files for this account (re-login refreshes in place).
    final_base = record.session_base
    is_refresh = final_base.with_suffix(".session").exists()
    users.cleanup_session_files(final_base)
    for suffix in (".session", ".session-journal"):
        src = tmp_base.with_suffix(suffix)
        if src.exists():
            src.rename(final_base.with_suffix(suffix))

    users.upsert_user(record)
    verb = "Refreshed session" if is_refresh else "Logged in"
    print(f"{verb} for {record.display_name} "
          f"(short_id={short_id}, user_id={me.id}).")


async def logout(short_id: str) -> None:
    """Revoke the given user's session server-side and delete the local files."""
    config.validate(require_bot=False)
    record = users.get_user(short_id)

    client = config.make_client(record.session_base)
    await client.connect()
    if await client.is_user_authorized():
        try:
            await client.log_out()
            print(f"Server-side session revoked for {record.short_id}.")
        except Exception as e:
            print(f"Server log_out failed (continuing with local cleanup): {e}")
    else:
        print(f"No active server-side session for {record.short_id}.")
    await client.disconnect()

    users.cleanup_session_files(record.session_base)
    users.remove_user(short_id)
    print(f"Removed user {short_id} ({record.display_name}).")


def list_users() -> None:
    """Print every registered user with their short_id, user_id, phone, and name."""
    db = users.load_db()
    if not db:
        print("No users registered. Run `login` to add one.")
        return

    header = (f"{'SHORT_ID':<10}{'USER_ID':<14}{'PHONE':<18}"
              f"{'NAME':<32}SESSION_FILE")
    print(header)
    print("-" * len(header))
    for sid, rec in db.items():
        phone = rec.phone or "(unknown)"
        if phone != "(unknown)" and not phone.startswith("+"):
            phone = f"+{phone}"
        print(f"{sid:<10}{rec.user_id:<14}{phone:<18}"
              f"{rec.display_name:<32}{rec.session_filename}")
