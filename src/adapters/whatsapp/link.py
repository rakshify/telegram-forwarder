"""CLI commands for WhatsApp accounts: wa-link, wa-unlink, wa-list-accounts,
wa-list-groups.

Only one connection per WhatsApp session can be open at a time. If the
bridge for an account is running, stop it first (`./forwarder.sh
wa-groups` does that for you), otherwise the two keep kicking each other off.
"""
import datetime
import sys
from typing import Optional

from ... import config
from . import accounts
from . import client as wa


def _remove_files(paths) -> None:
    for p in paths:
        if p.exists():
            p.unlink()


async def _logout_quietly(account: accounts.WaAccount) -> bool:
    """Log the stored session out (removes it from the phone's Linked devices)."""
    try:
        client = await wa.connect_ready(account.db_path, timeout=60)
    except SystemExit as e:
        print(f"  couldn't connect to log out: {e}")
        return False
    try:
        await client.logout()
        return True
    except Exception as e:
        print(f"  logout failed: {e}")
        return False
    finally:
        await wa.stop(client)


async def link(phone: Optional[str], force: bool = False) -> None:
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    wa.quiet_logs()
    if not phone:
        if not sys.stdin.isatty():
            raise SystemExit("Pass the number: wa-link --phone +<country code><number>")
        phone = input("WhatsApp number with + and country code (e.g. +919876543210): ").strip()
    digits = accounts.normalize_phone(phone)
    acct = accounts.WaAccount(short_id=accounts.make_short_id(digits), phone=digits)
    previous = accounts.load_db().get(acct.short_id)

    if wa.stored_device(acct.db_path) is not None:
        if not force:
            print(f"+{digits} is already linked (short_id={acct.short_id}). "
                  f"To link it again, run wa-link with --force.")
            return
        print("Logging the old link out first...")
        if not await _logout_quietly(acct):
            print("  Remove the old 'Telegram bridge' entry under Linked devices on the phone by hand.")
    # Start from a clean session file (also clears a half-finished earlier attempt).
    _remove_files(f for f in accounts.session_files(acct) if f.name.startswith(f"wa_{acct.short_id}.db"))

    print(f"Linking WhatsApp +{digits} (short_id={acct.short_id}).")
    print("On the phone, open: WhatsApp > Settings > Linked devices > Link a device >\n"
          "  'Link with phone number instead'.", flush=True)

    def show_code(code: str, attempt: int) -> None:
        pretty = f"{code[:4]}-{code[4:]}" if len(code) == 8 and "-" not in code else code
        print(f"\n    PAIRING CODE:  {pretty}\n", flush=True)
        print("Type it on the phone within about 2 minutes (WhatsApp also shows a "
              "notification you can tap).", flush=True)

    me = await wa.link(acct.db_path, digits, show_code)
    acct.jid = wa.jid_str(me.JID) or f"{digits}@s.whatsapp.net"
    acct.push_name = me.PushName or (previous.push_name if previous else None)
    acct.linked_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    accounts.upsert(acct)
    print(f"\nLinked {acct.display} (short_id={acct.short_id}).")
    print(f"Next: list its groups with   ./forwarder.sh wa-groups {acct.short_id}")


async def unlink(short_id: str) -> None:
    wa.quiet_logs()
    acct = accounts.get_account(short_id)
    if wa.stored_device(acct.db_path) is not None:
        if await _logout_quietly(acct):
            print("Removed the linked device from the phone.")
        else:
            print("Remove 'Telegram bridge' under Linked devices on the phone by hand.")
    _remove_files(accounts.session_files(acct))
    accounts.remove(acct.short_id)
    print(f"Unlinked {acct.display} (short_id={acct.short_id}).")


def list_accounts() -> None:
    db = accounts.load_db()
    if not db:
        print("No WhatsApp accounts linked. Run `wa-link` to add one.")
        return
    header = f"{'SHORT_ID':<10}{'PHONE':<18}{'NAME':<28}{'LINKED_AT':<27}SESSION"
    print(header)
    print("-" * len(header))
    for sid, acct in db.items():
        state = "ok" if wa.stored_device(acct.db_path) is not None else "NOT LINKED"
        print(f"{sid:<10}{'+' + acct.phone:<18}{(acct.push_name or '(no name)'):<28}"
              f"{(acct.linked_at or '?'):<27}{state}")


async def list_groups(short_id: str) -> None:
    wa.quiet_logs()
    acct = accounts.get_account(short_id)
    if wa.stored_device(acct.db_path) is None:
        raise SystemExit(f"{acct.display} isn't linked. Run wa-link.")
    client = await wa.connect_ready(acct.db_path)
    try:
        rows = await wa.joined_groups(client)
    finally:
        await wa.stop(client)

    print(f"WhatsApp groups of {acct.display} (short_id={acct.short_id})")
    header = f"{'KIND':<15}{'GROUP_ID':<36}{'MEMBERS':<9}NAME"
    print(header)
    print("-" * (len(header) + 30))
    order = {"community": 0, "announcements": 1, "group": 2}
    for r in sorted(rows, key=lambda r: (order.get(r.kind, 3), r.name.lower())):
        print(f"{r.kind:<15}{r.jid:<36}{r.size:<9}{r.name}")
    if not rows:
        print("(no groups)")
    print("\nPut a GROUP_ID in configs/wa-<short_id>.json as a pair's \"source\". "
          "A community itself carries no messages:\nuse its announcements group or its groups.")
