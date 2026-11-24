"""Command-line entry point for the forwarder.

Subcommands
-----------
login         Interactive login. After success, prints the new short_id.
logout        Revoke and delete a specific user's session (-u SHORT_ID).
list-users    Print every registered user with short_id, user_id, phone, name.
list-groups   Print every group/channel a specific user is in (-u SHORT_ID).
forward       Run the forwarder a config describes (-c CONFIG), whatever its
              source (Telegram or WhatsApp); or Telegram pairs given as flags,
              read with a specific user's session (-u SHORT_ID).

WhatsApp (src/adapters/whatsapp; neonize is only imported by these commands)
wa-link           Link a WhatsApp account with a pairing code.
wa-unlink         Log a linked WhatsApp account out and delete its files.
wa-list-accounts  Print every linked WhatsApp account.
wa-list-groups    Print the WhatsApp groups of an account, with their ids.
wa-forward        `forward` for a WhatsApp config (-c CONFIG [-a SHORT_ID]).
"""
import argparse
import asyncio
import sys

from . import auth
from . import list_groups as lg


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-forwarder",
        description="Forward messages from N Telegram chats to N mapped chats.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "login",
        help="Interactive login (phone number + OTP + optional 2FA password).",
    )

    sub.add_parser(
        "list-users",
        help="List every registered user with short_id, user_id, phone, and name.",
    )

    p_logout = sub.add_parser(
        "logout",
        help="Revoke a user's session server-side and delete their local session files.",
    )
    p_logout.add_argument(
        "-u", "--user", required=True, metavar="SHORT_ID",
        help="Short ID of the user to log out (see `list-users`).",
    )

    p_groups = sub.add_parser(
        "list-groups",
        help="List every group/channel a specific user is in along with its id and access_hash.",
    )
    p_groups.add_argument(
        "-u", "--user", required=True, metavar="SHORT_ID",
        help="Short ID of the user whose session to use (see `list-users`).",
    )

    p_topics = sub.add_parser(
        "list-topics",
        help="List topics inside a forum-enabled supergroup (community).",
    )
    p_topics.add_argument(
        "-u", "--user", required=True, metavar="SHORT_ID",
        help="Short ID of the user whose session to use.",
    )
    p_topics.add_argument(
        "-gid", "--group_chat_id", required=True, metavar="ID",
        help="Parent supergroup id (e.g. -1001234567890).",
    )

    p_clone = sub.add_parser(
        "clone-topics",
        help="Create matching topics in DST community for every topic in SRC. "
             "User must be admin in DST with manage-topics permission.",
    )
    p_clone.add_argument(
        "-u", "--user", required=True, metavar="SHORT_ID",
        help="Short ID of the user whose session to use.",
    )
    p_clone.add_argument(
        "-gid", "--group_chat_id", required=True, metavar="SRC_ID",
        help="Source community id to copy topics from.",
    )
    p_clone.add_argument(
        "-mid", "--mapped_chat_id", required=True, metavar="DST_ID",
        help="Destination community id to create topics in.",
    )

    p_forward = sub.add_parser(
        "forward",
        help="Run a forwarder: a config file (any source), or Telegram pairs as flags.",
    )
    p_forward.add_argument(
        "-u", "--user", required=False, metavar="SHORT_ID", default=None,
        help="Short ID of the account whose session reads the sources (a Telegram user, "
             "or a WhatsApp account for a WhatsApp config). Required unless the config "
             "file (--config) sets `account`.",
    )
    p_forward.add_argument(
        "-c", "--config", metavar="PATH", default=None,
        help="Path to a JSON config file describing pairs (README section 6). Mutually "
             "exclusive with the per-flag pair arguments below.",
    )
    p_forward.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be posted instead of posting it; saves no state. Stop the "
             "running forwarder for the same account first.",
    )
    p_forward.add_argument(
        "-gid", "--group_chat_id",
        nargs="+", required=False, metavar="ID",
        help="Source chat IDs (e.g. -1001234567890). Space-separated for multiple.",
    )
    p_forward.add_argument(
        "-gh", "--group_chat_hash",
        nargs="+", required=False, metavar="HASH",
        help="Source chat access_hashes, in the same order as -gid. Use 0 for basic groups.",
    )
    p_forward.add_argument(
        "-mid", "--mapped_chat_id",
        nargs="+", required=False, metavar="ID",
        help="Destination chat IDs, 1:1 mapped to -gid by position.",
    )
    p_forward.add_argument(
        "-mh", "--mapped_chat_hash",
        nargs="+", required=False, metavar="HASH",
        help="Destination chat access_hashes (kept for symmetry; bot resolves its own at runtime).",
    )
    p_forward.add_argument(
        "-tid", "--topic_id",
        nargs="+", default=None, metavar="TOPIC_ID",
        help="Optional per-pair source topic ids (1:1 with -gid). Use 0 for whole chat. "
             "Omit entirely to disable source topic filtering for all pairs.",
    )
    p_forward.add_argument(
        "-mtid", "--mapped_topic_id",
        nargs="+", default=None, metavar="TOPIC_ID",
        help="Optional per-pair destination topic ids (1:1 with -mid). Use 0 to post in "
             "the main feed. Omit entirely to disable destination topic routing for all pairs.",
    )
    p_forward.add_argument(
        "-attr", "--attribution",
        action="store_true",
        help="Prefix each forwarded message with the source sender's name (and "
             "@username, when present). Applies to all pairs in this invocation. "
             "For per-pair control, use a config file with `-c`.",
    )

    # ---------------------------------------------------------------- WhatsApp
    p_wlink = sub.add_parser(
        "wa-link",
        help="Link a WhatsApp account: prints a pairing code to type into the phone.",
    )
    p_wlink.add_argument(
        "--phone", metavar="+NUMBER", default=None,
        help="WhatsApp number with + and country code, e.g. +919876543210 "
             "(asked interactively when omitted).",
    )
    p_wlink.add_argument(
        "--force", action="store_true",
        help="Log out and replace an existing link for this number.",
    )

    sub.add_parser("wa-list-accounts", help="List linked WhatsApp accounts.")

    p_wgroups = sub.add_parser(
        "wa-list-groups",
        help="List the WhatsApp groups of an account with their ids (stop its bridge first).",
    )
    p_wgroups.add_argument("-a", "--account", required=True, metavar="SHORT_ID",
                           help="Short id from `wa-list-accounts`.")

    p_wunlink = sub.add_parser(
        "wa-unlink",
        help="Log a WhatsApp account out (removes it from the phone) and delete its files.",
    )
    p_wunlink.add_argument("-a", "--account", required=True, metavar="SHORT_ID",
                           help="Short id from `wa-list-accounts`.")

    p_wfwd = sub.add_parser(
        "wa-forward",
        help="`forward` for a WhatsApp config: WhatsApp chats to Telegram or Signal chats.",
    )
    p_wfwd.add_argument("-c", "--config", required=True, metavar="PATH",
                        help="Path to the WhatsApp config, e.g. /app/configs/wa-<short_id>.json.")
    p_wfwd.add_argument("-a", "--account", default=None, metavar="SHORT_ID",
                        help="Account short id, if the config doesn't set `account`.")
    p_wfwd.add_argument("--dry-run", action="store_true",
                        help="Log what would be posted instead of posting it.")

    return parser


async def _amain(args: argparse.Namespace) -> None:
    if args.command == "login":
        await auth.login()
    elif args.command == "logout":
        await auth.logout(args.user)
    elif args.command == "list-users":
        auth.list_users()
    elif args.command == "list-groups":
        await lg.list_groups(args.user)
    elif args.command == "list-topics":
        await lg.list_topics(args.user, args.group_chat_id)
    elif args.command == "clone-topics":
        await lg.clone_topics(args.user, args.group_chat_id, args.mapped_chat_id)
    elif args.command == "forward":
        from pathlib import Path
        from .adapters import routes
        if args.config:
            cfg = await routes.load(Path(args.config), account=args.user)
        else:
            from .adapters.telegram import config_file as tg_config
            if not args.user:
                raise SystemExit("forward: -u/--user is required unless --config is used.")
            for required, name in [
                (args.group_chat_id, "-gid"),
                (args.group_chat_hash, "-gh"),
                (args.mapped_chat_id, "-mid"),
                (args.mapped_chat_hash, "-mh"),
            ]:
                if not required:
                    raise SystemExit(f"forward: {name} is required unless --config is used.")
            cfg = tg_config.from_cli(
                args.user, args.group_chat_id, args.group_chat_hash, args.mapped_chat_id,
                args.mapped_chat_hash, args.topic_id, args.mapped_topic_id,
                bool(getattr(args, "attribution", False)),
            )
        code = await routes.run(cfg, dry_run=args.dry_run)
        if code:
            raise SystemExit(code)
    elif args.command.startswith("wa-"):
        await _wa_command(args)


async def _wa_command(args: argparse.Namespace) -> None:
    # Imported here so the Telegram commands never load neonize.
    from pathlib import Path
    from .adapters.whatsapp import link as wa_link

    if args.command == "wa-link":
        await wa_link.link(args.phone, force=args.force)
    elif args.command == "wa-list-accounts":
        wa_link.list_accounts()
    elif args.command == "wa-list-groups":
        await wa_link.list_groups(args.account)
    elif args.command == "wa-unlink":
        await wa_link.unlink(args.account)
    elif args.command == "wa-forward":
        from .adapters import routes
        from .core.model import WHATSAPP
        cfg = await routes.load(Path(args.config), platform=WHATSAPP, account=args.account)
        code = await routes.run(cfg, dry_run=args.dry_run)
        if code:
            raise SystemExit(code)


def main() -> None:
    args = _build_parser().parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
