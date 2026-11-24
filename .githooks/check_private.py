#!/usr/bin/env python3
"""Keeps private data out of git: real configs, env files, session state, credentials.

Run by .githooks/pre-commit (what's staged) and .githooks/pre-push (every
commit about to be pushed). Turn the hooks on once per clone:

    git config core.hooksPath .githooks

It refuses:
* private files, by path: configs/* except README.md and *.example.json;
  env files (.env, env, *.env, .env.*) except .env.example; anything under
  data/ or sessions/; Telethon sessions and SQLite state;
* credential-shaped text in any file: Telegram bot tokens, API hashes,
  private keys, AWS access keys;
* any value from your own private files, pasted into any file: the chat ids,
  access hashes, phone numbers and group ids in configs/*.json, and the
  secret values in env files. That is how real chat ids once ended up in
  the docs and tests.

Findings name the file and the kind of value, never the value itself.
By hand:

    python3 .githooks/check_private.py --tracked   # the files in this checkout
    python3 .githooks/check_private.py --history   # every commit on every branch

Standard library only, Python 3.8+ (hooks run on the host, not in the image).
"""
import functools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

ZERO = "0" * 40
MIN_LEN = 8  # shorter values (topic ids, hash 0, short ids) are too common to match on

CREDENTIALS = [
    ("a Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("a Telegram API hash", re.compile(r"(?i)api_?hash\W{0,4}[0-9a-f]{32}\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
]
SECRET_KEY = re.compile(r"(?i)TOKEN|HASH|SECRET|PASSWORD|API_ID|KEY|SENDER|PHONE")
CONFIG_KEYS = {"source", "source_hash", "dest", "dest_hash", "alerts"}


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


@functools.lru_cache(maxsize=None)
def root() -> Path:
    return Path(git("rev-parse", "--show-toplevel").decode().strip())


def is_env_file(name: str) -> bool:
    return name != ".env.example" and (name in (".env", "env") or name.endswith(".env")
                                       or name.startswith(".env."))


def private_path(path: str) -> Optional[str]:
    """The kind of private file `path` is, or None if it may be committed."""
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    top = p.split("/", 1)[0]
    if top in ("data", "sessions"):
        return "session data"
    if top == "configs" and name != "README.md" and not name.endswith(".example.json"):
        return "a real config"
    if is_env_file(name):
        return "an env file"
    if name.endswith((".session", ".session-journal")):
        return "a Telegram session"
    if name.endswith((".sqlite", ".sqlite-wal", ".sqlite-shm", ".db")):
        return "a state database"
    return None


def private_values(base: Optional[Path] = None) -> Dict[str, str]:
    """value -> where it comes from, read from the private files in this checkout."""
    base = base or root()
    found: Dict[str, str] = {}

    def add(raw, where: str) -> None:
        v = str(raw).strip().strip("\"'")
        if len(v) >= MIN_LEN:
            found.setdefault(v, where)
        bare = v[4:] if v.startswith("-100") else v[1:] if v.startswith("-") else ""
        if bare.isdigit() and len(bare) >= MIN_LEN:  # Telegram ids are also written without the -100
            found.setdefault(bare, where)

    configs = base / "configs"
    for f in sorted(configs.rglob("*") if configs.is_dir() else []):
        rel = f.relative_to(base).as_posix()
        if not f.is_file() or private_path(rel) is None:
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue

        def walk(node, key: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if not str(k).startswith("_"):
                        walk(v, str(k))
            elif isinstance(node, list):
                for v in node:
                    walk(v, key)
            elif key in CONFIG_KEYS or (isinstance(node, str)
                                        and (node.startswith(("+", "group.")) or "@" in node)):
                add(node, f"a value from {rel}")
        walk(data)

    for f in sorted(base.rglob("*")):
        if ".git" in f.relative_to(base).parts or not is_env_file(f.name) or not f.is_file():
            continue
        try:
            lines = f.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                if SECRET_KEY.search(key):
                    add(value, f"a secret from {f.relative_to(base).as_posix()}")
    return found


def problems(path: str, content: bytes, values: Dict[str, str]) -> List[str]:
    """What's private about one file version, as messages without the values."""
    kind = private_path(path)
    if kind:
        return [f"{path} is {kind}"]
    text = content.decode("utf-8", "replace")
    found = [f"{path} contains {what}" for what, pattern in CREDENTIALS if pattern.search(text)]
    found += [f"{path} contains {where}" for where in sorted({w for v, w in values.items() if v in text})]
    return found


# ---------------------------------------------------------------------- what to look at

Version = Tuple[str, str, bytes]  # (commit it comes from, or "", path, content)


def staged() -> Iterable[Version]:
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z").decode().split("\0")
    for name in filter(None, names):
        yield "", name, git("show", f":{name}")


def tracked() -> Iterable[Version]:
    for name in filter(None, git("ls-files", "-z").decode().split("\0")):
        f = root() / name
        if f.is_file():
            yield "", name, f.read_bytes()


def in_commits(commits: List[str]) -> Iterable[Version]:
    """Every file version these commits add, each once, with the commit that added it."""
    seen: Set[str] = set()
    for commit in commits:
        out = git("diff-tree", "-r", "-m", "--root", "--no-commit-id", "--no-renames", "-z",
                  "--diff-filter=ACMRT", commit).decode().split("\0")
        for meta, path in zip(out[0::2], out[1::2]):
            sha = meta.split()[3] if meta.startswith(":") else ""
            if sha and sha not in seen:
                seen.add(sha)
                yield commit[:7], path, git("cat-file", "blob", sha)


def pushed(refs: str) -> List[str]:
    """The commits a push would send: everything new to the remote for each ref."""
    commits: List[str] = []
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:  # malformed, or deleting a branch
            continue
        local, remote = parts[1], parts[3]
        known = remote != ZERO and subprocess.run(["git", "cat-file", "-e", remote],
                                                  capture_output=True).returncode == 0
        span = [local, f"^{remote}"] if known else [local]  # new on the remote: all of it
        commits += [c for c in git("rev-list", *span).decode().split() if c not in commits]
    return commits


# ---------------------------------------------------------------------- main

ADVICE = {
    "--staged": "Unstage the file (git restore --staged <file>), or put a placeholder where the "
                "value is. If it's a false alarm: git commit --no-verify.",
    "--push": "Those commits carry it. Take it out of history before pushing (git rebase -i, "
              "or git filter-repo for older commits). If it's a false alarm: git push --no-verify.",
}


def main(argv: List[str]) -> int:
    mode = argv[0] if argv else ""
    if mode == "--staged":
        files = staged()
    elif mode == "--push":
        files = in_commits(pushed(sys.stdin.read()))
    elif mode == "--tracked":
        files = tracked()
    elif mode == "--history":
        files = in_commits(git("rev-list", "--all").decode().split())
    else:
        print(__doc__)
        return 2
    values = private_values()
    found = []
    for commit, path, content in files:
        found += [f"{commit} {p}" if commit else p for p in problems(path, content, values)]
    if not found:
        if mode in ("--tracked", "--history"):
            print(f"No private files or values ({len(values)} private values checked).")
        return 0
    print("Private data would leave this machine:" if mode in ADVICE else "Private data found:",
          file=sys.stderr)
    for line in found:
        print(f"  {line}", file=sys.stderr)
    if mode in ADVICE:
        print(ADVICE[mode], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
