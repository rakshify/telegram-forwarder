"""Command-line entry point for the forwarder.

Subcommands
-----------
login         Interactive login. After success, prints the new short_id.
logout        Revoke and delete a specific user's session (-u SHORT_ID).
list-users    Print every registered user with short_id, user_id, phone, name.
list-groups   Print every group/channel a specific user is in (-u SHORT_ID).
forward       Forward N source chats to N destination chats (1:1 mapped),
              using the session of a specific user (-u SHORT_ID).
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
        help="Forward N source chats to N destination chats (1:1 by position).",
    )
    p_forward.add_argument(
        "-u", "--user", required=False, metavar="SHORT_ID", default=None,
        help="Short ID of the user whose session to use as the source listener. "
             "Required unless the config file (--config) sets `user`.",
    )
    p_forward.add_argument(
        "-c", "--config", metavar="PATH", default=None,
        help="Path to a JSON config file describing pairs. Mutually exclusive "
             "with the per-flag pair arguments below.",
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

    return parser


def _build_pairs(args: argparse.Namespace) -> List[fwd.ChatPair]:
    gids: List[str] = args.group_chat_id
    ghs: List[str] = args.group_chat_hash
    mids: List[str] = args.mapped_chat_id
    mhs: List[str] = args.mapped_chat_hash
    tids: List[str] = args.topic_id if args.topic_id is not None else ["0"] * len(gids)
    mtids: List[str] = (
        args.mapped_topic_id if args.mapped_topic_id is not None else ["0"] * len(gids)
    )

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

    return [
        fwd.ChatPair(
            source_id=fwd.parse_chat_id(gid),
            source_hash=int(gh),
            dest_id=fwd.parse_chat_id(mid),
            dest_hash=int(mh),
            topic_id=int(tid),
            dest_topic_id=int(mtid),
        )
        for gid, gh, mid, mh, tid, mtid in zip(gids, ghs, mids, mhs, tids, mtids)
    ]


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
        if args.config:
            from . import config_file
            from pathlib import Path
            short_id, pairs = await config_file.load_config(
                Path(args.config), fallback_short_id=args.user
            )
            await fwd.forward_pairs(short_id, pairs)
        else:
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
            pairs = _build_pairs(args)
            await fwd.forward_pairs(args.user, pairs)


def main() -> None:
    args = _build_parser().parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
