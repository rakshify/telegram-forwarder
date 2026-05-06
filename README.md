# telegram-forwarder

Forward messages from **N Telegram chats** to **N mapped Telegram chats** (1:1) using a Telegram bot. Built with [Telethon](https://docs.telethon.dev/), packaged for Docker, and ready to deploy to AWS / GCP / any container runtime.

It supports text, photos, videos, voice messages, video notes, GIFs, audio files, generic documents, and polls — replies in the source chat appear as proper replies in the destination — and it backfills missed messages on startup if it was offline.

---

## Features

- **Multiple user accounts.** Log in any number of Telegram user accounts. Each is identified by a short, deterministic 6-character ID derived from the account's Telegram user id.
- **N → N forwarding.** One source chat per destination, by position. Run as many pairs as you like in a single process.
- **Reply preservation.** Replies in the source chat become replies in the destination, via a per-pair message-id map.
- **Catch-up on restart.** Each pair's last-forwarded source message id is persisted. When the forwarder starts, it backfills anything that arrived while it was offline before going live.
- **All common media types.** Polls are reconstructed; everything else is downloaded by the user account and re-uploaded by the bot.
- **Container-ready.** Persistent volume for sessions and state, env-var config, no host paths baked in.

---

## Project layout

```
telegram-forwarder/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
├── .env.example
├── README.md
└── src/
    ├── main.py            # argparse CLI
    ├── config.py          # env vars + paths
    ├── auth.py            # login / logout / list-users
    ├── users.py           # users.json registry, short_id derivation
    ├── state.py           # forward_state.json (last-seen msg id per pair)
    ├── list_groups.py     # list-groups -u <short_id>
    └── forwarder.py       # forward -u <short_id> with catch-up
```

The persistent volume holds:

```
/data/sessions/
├── users.json                  # registry of logged-in accounts
├── forward_state.json          # last-forwarded msg id per pair
├── user_<short_id>.session     # per-user Telethon session (one per account)
├── bot_session.session         # bot session (single bot, shared)
└── …
```

---

## 1. Configure

Get your API credentials at <https://my.telegram.org/apps>, and create a bot via [@BotFather](https://t.me/BotFather).

```bash
cp .env.example .env
# edit .env and fill in:
#   TG_API_ID
#   TG_API_HASH
#   BOT_TOKEN
```

The bot must be a member of every destination chat with permission to send messages.

---

## 2. Build the image

```bash
docker compose build
```

---

## 3. Log in (one or more user accounts)

The login flow needs a TTY. Use `docker compose run` (not `up`) to get one:

```bash
docker compose run --rm forwarder login
```

You'll be asked for:
1. Phone number with country code (e.g. `+14155551234`)
2. The OTP that Telegram sends you
3. Your 2FA password — only if you have 2FA enabled

After success, the command prints something like:

```
Logged in for Rakshit @rakshify (short_id=15e2b0, user_id=123456789).
```

**Save the `short_id` — that's how you'll reference this user in every other command.** Run `login` again for any additional accounts.

---

## 4. List logged-in users

```bash
docker compose run --rm forwarder list-users
```

```
SHORT_ID  USER_ID       PHONE             NAME                            SESSION_FILE
15e2b0    123456789     +14155551234      Rakshit @rakshify               user_15e2b0.session
8a9bcf    987654321     +919876543210     Other Account @other            user_8a9bcf.session
```

---

## 5. Log out a specific user

Revokes the session server-side and deletes the local files for that user only:

```bash
docker compose run --rm forwarder logout -u 15e2b0
```

---

## 6. Flow 1 — list groups for a specific user

```bash
docker compose run --rm forwarder list-groups -u 15e2b0
```

```
TYPE       CHAT_ID           ACCESS_HASH           TITLE
supergroup -1001234567890    1234567890123456789   My source group
group      -9876543210       0                     My basic source group
supergroup -1005555555555    5555555555555555555   My destination group
```

Basic groups print `0` for the access_hash — pass `0` literally for `-gh` / `-mh` and the tool will route them through `InputPeerChat` automatically.

---

## 7. Flow 2 — forward N → N as a specific user

```bash
docker compose run --rm forwarder forward \
  -u 15e2b0 \
  -gid -1001234567890 -1009876543210 \
  -gh  1234567890123456789 9876543210987654321 \
  -mid -1005555555555 -1006666666666 \
  -mh  0 0
```

| flag | long form | meaning |
|---|---|---|
| `-u`   | `--user` | Short ID of the user account to listen with. |
| `-gid` | `--group_chat_id` | Source chat IDs (space-separated). |
| `-gh`  | `--group_chat_hash` | Source access_hashes, **same order as `-gid`**. `0` for basic groups. |
| `-mid` | `--mapped_chat_id` | Destination chat IDs, **1:1 mapped to `-gid` by position**. |
| `-mh`  | `--mapped_chat_hash` | Destination access_hashes (kept for symmetry; the bot resolves its own at runtime). |

So in the example above, messages from `-1001234567890` go to `-1005555555555`, and messages from `-1009876543210` go to `-1006666666666`, with the user account `15e2b0` as the source-side listener.

### Catch-up behaviour

On every startup, for every `(user, source, dest)` triple, the forwarder looks up the last source message id it forwarded, fetches everything newer via `iter_messages(min_id=last_seen)`, and forwards them in chronological order before attaching the live listener. State is persisted in `forward_state.json` after every successful forward.

The very first run for a brand-new pair establishes a baseline at the **current latest message id** — it does not backfill the entire chat history. From the next run onwards, it will catch up only what was missed during downtime.

If a catch-up forward fails (e.g. transient network error, malformed media), the state is **not** advanced past that message — the next run retries from the same point.

### Run as a long-running container

For production, edit `docker-compose.yml` and replace the `command:` block with your specific args. Then:

```bash
docker compose up -d
docker compose logs -f forwarder
```

---

## Migrating from the single-user version

If you previously had a `user_session.session` from the single-user version of this tool, it isn't picked up automatically. Either:

```bash
# clean slate
rm data/sessions/user_session.session data/sessions/user_session.session-journal
docker compose run --rm forwarder login
```

Then re-run `forward` with the new `-u` argument. The `forward_state.json` will be created fresh on first run.

---

## Cloud deployment

The image is a single-process container with stateful sessions. Anywhere you can run a container with a persistent volume works.

**AWS EC2 (simplest):** `docker compose up -d` on a `t3.micro`. The bind-mounted `./data/sessions` directory is your state — back it up if you care about not re-doing logins.

**AWS ECS (Fargate):** push to ECR, create a task definition, attach an EFS volume mounted at `/data/sessions`. Don't run more than one task pointed at the same EFS — the SQLite session files only support a single writer.

**GCP Cloud Run / GKE:** Cloud Run works for the long-running forwarder with min instances = 1; mount Filestore for sessions. GKE: a `Deployment` with a `PersistentVolumeClaim` on `/data/sessions`, env vars from a `Secret`.

---

## Local development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill values
export SESSION_DIR=./sessions
python -m src.main login
python -m src.main list-users
python -m src.main list-groups -u <short_id>
python -m src.main forward -u <short_id> -gid -100... -gh ... -mid -100... -mh ...
```

---

## How it works

- **User client** (one of your phone-number-authenticated sessions, selected by `-u`) listens for `NewMessage` events on each source chat.
- **Bot client** (the BotFather token) sends to each destination chat, keeping your personal account out of destination groups.
- **Per-pair state.** `forward_state.json` keeps `{user_short_id}:{source_id}:{dest_id} -> last_msg_id`. Persisted after every successful forward.
- **Catch-up.** On startup, for each pair: `iter_messages(source, min_id=last_seen)` returns newest-first, the code reverses to chronological order and forwards each through the bot before attaching the live listener.
- **Live listener.** A `NewMessage` handler per source skips any message id at or below the current state value (covers the brief overlap window between catch-up and listener attach), forwards via the bot, and advances state.
- **Reply mapping.** Per-pair `msg_id_map` records every source-id → destination-id. Reply messages look up the mapped destination id and use it as `reply_to`. The map is in-memory; replies pointing at messages from a previous run won't be linked but will still send as standalone messages.
- **Per-account access hashes.** Telegram access_hashes are per-account. The user account uses its own hash (from `list-groups`) for source supergroups; the bot always re-resolves its destination via `get_entity` so it gets the bot's own hash.

---

## Limitations

- Stickers are forwarded as `.webp` files; bots have restricted sticker-send capabilities.
- Service messages (joins, pins, etc.) are skipped.
- Edits and deletions in the source aren't propagated — only `NewMessage` is handled.
- The reply mapping is in-memory: replies in live messages that point to messages from a previous process lifetime won't be linked as replies (they're sent as standalone messages).
- The bot must be a member of every destination chat with permission to post.

---

## Troubleshooting

- **`No user with short id '…'`** — run `list-users` to see registered users; `login` if needed.
- **`User <id> session not authorized`** — the session file was deleted or revoked. Run `login` again for that account.
- **`Bot could not resolve dest chat`** — the bot isn't in the destination, or doesn't have permission. Add it manually first.
- **Login asks again every time** — make sure `./data/sessions` is mounted into the container. `docker compose config` shows resolved volumes.
- **2FA never prompted but you have 2FA on** — you're not running with `-it`. Use `docker compose run --rm forwarder login` (not `exec`, not background `up`).
