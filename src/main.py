"""Command-line entry point for the forwarder.

Subcommands
-----------
login         Interactive login (phone, OTP, optional 2FA).
logout        Revoke the user session and delete local session files.
list-groups   Print every group/channel the user is in with id and access_hash.
forward       Forward N source chats to N destination chats (1:1 mapped).
"""
import argparse
import asyncio
import sys
from typing import List

from . import auth
from . import forwarder as fwd
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
        "logout",
        help="Revoke the user session server-side and delete local session files.",
    )
    sub.add_parser(
        "list-groups",
        help="List every group/channel the logged-in user is in along with its id and access_hash.",
    )

    forward = sub.add_parser(
        "forward",
        help="Forward N source chats to N destination chats (1:1 by position).",
    )
    forward.add_argument(
        "-gid", "--group_chat_id",
        nargs="+", required=True, metavar="ID",
        help="Source chat IDs (e.g. -1001234567890). Space-separated for multiple.",
    )
    forward.add_argument(
        "-gh", "--group_chat_hash",
        nargs="+", required=True, metavar="HASH",
        help="Source chat access_hashes, in the same order as -gid.",
    )
    forward.add_argument(
        "-mid", "--mapped_chat_id",
        nargs="+", required=True, metavar="ID",
        help="Destination chat IDs, 1:1 mapped to -gid by position.",
    )
    forward.add_argument(
        "-mh", "--mapped_chat_hash",
        nargs="+", required=True, metavar="HASH",
        help="Destination chat access_hashes, in the same order as -mid.",
    )

    return parser


def _build_pairs(args: argparse.Namespace) -> List[fwd.ChatPair]:
    gids: List[str] = args.group_chat_id
    ghs: List[str] = args.group_chat_hash
    mids: List[str] = args.mapped_chat_id
    mhs: List[str] = args.mapped_chat_hash

    n = len(gids)
    if not (len(ghs) == len(mids) == len(mhs) == n):
        raise SystemExit(
            "Length mismatch: -gid, -gh, -mid, -mh must all have the same number of values "
            f"(got {len(gids)}, {len(ghs)}, {len(mids)}, {len(mhs)})."
        )
    if n == 0:
        raise SystemExit("At least one source/destination pair is required.")

    pairs: List[fwd.ChatPair] = []
    for gid, gh, mid, mh in zip(gids, ghs, mids, mhs):
        pairs.append(
            fwd.ChatPair(
                source_id=fwd.parse_chat_id(gid),
                source_hash=int(gh),
                dest_id=fwd.parse_chat_id(mid),
                dest_hash=int(mh),
            )
        )
    return pairs


async def _amain(args: argparse.Namespace) -> None:
    if args.command == "login":
        await auth.login()
    elif args.command == "logout":
        await auth.logout()
    elif args.command == "list-groups":
        await lg.list_groups()
    elif args.command == "forward":
        pairs = _build_pairs(args)
        await fwd.forward_pairs(pairs)


def main() -> None:
    args = _build_parser().parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
