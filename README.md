# telegram-forwarder

Forward messages from **N Telegram chats** to **N mapped Telegram chats** (1:1) using a Telegram bot. Built with [Telethon](https://docs.telethon.dev/), packaged for Docker, and ready to deploy to AWS / GCP / any container runtime.

It supports text, photos, videos, voice messages, video notes, GIFs, audio files, generic documents, and polls — and replies in the source chat appear as proper replies in the destination.

---

## Features

- **N → N forwarding.** One source chat per destination, by position. Run as many pairs as you like in a single process.
- **Reply preservation.** A per-pair message-id map ensures replies in the destination point at the right message.
- **All common media types.** Polls are reconstructed; everything else is downloaded by the user account and re-uploaded by the bot.
- **Two clear flows:** one to discover chat IDs/access-hashes, one to run the forwarder.
- **Container-ready.** Persistent volume for sessions, env-var config, no host paths baked in.
- **Logout / kill-session command** to revoke the user session on Telegram and wipe local session files.

---

## Project layout

```
telegram-forwarder/
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── src/
    ├── __init__.py
    ├── main.py            # argparse CLI (login / logout / list-groups / forward)
    ├── config.py          # env vars + session paths
    ├── auth.py            # interactive login + logout (kill session)
    ├── list_groups.py     # Flow 1: print id + access_hash for every group/channel
    └── forwarder.py       # Flow 2: forward N source chats to N destination chats
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

**Important:** the bot must be a member of every destination chat, with permission to send messages. Add it manually first.

---

## 2. Build the image

```bash
docker compose build
```

Sessions live on the host at `./data/sessions/` (mounted into the container at `/data/sessions`). They survive container restarts and image rebuilds.

---

## 3. Log in (interactive — phone, OTP, optional 2FA)

The login flow needs a TTY for the prompts. Use `docker compose run` (not `up`) so STDIN is attached:

```bash
docker compose run --rm forwarder login
```

You'll be asked for:

1. Phone number with country code (e.g. `+14155551234`)
2. The OTP that Telegram sends you
3. Your 2FA password — only if you have 2FA enabled

The session is saved at `./data/sessions/user_session.session` on the host.

---

## 4. Flow 1 — list groups (get chat IDs + access hashes)

```bash
docker compose run --rm forwarder list-groups
```

Prints something like:

```
TYPE       CHAT_ID           ACCESS_HASH           TITLE
-------------------------------------------------------------
supergroup -1001234567890    1234567890123456789   My source group
channel    -1009876543210    9876543210987654321   Some channel
supergroup -1005555555555    5555555555555555555   My destination group
```

Save the IDs and access-hashes — you'll plug them into the forward command. Basic (legacy) groups print `0` for the access_hash; pass `0` literally for `-gh` / `-mh` and the tool will route them through `InputPeerChat` automatically.

---

## 5. Flow 2 — forward N → N

```bash
docker compose run --rm forwarder forward \
  -gid -1001234567890 -1009876543210 \
  -gh  1234567890123456789 9876543210987654321 \
  -mid -1005555555555 -1006666666666 \
  -mh  5555555555555555555 6666666666666666666
```

| flag | long form | meaning |
|---|---|---|
| `-gid` | `--group_chat_id` | Source chat IDs (space-separated). |
| `-gh`  | `--group_chat_hash` | Source access_hashes, **same order as `-gid`**. |
| `-mid` | `--mapped_chat_id` | Destination chat IDs, **1:1 mapped to `-gid` by position**. |
| `-mh`  | `--mapped_chat_hash` | Destination access_hashes, **same order as `-mid`**. |

So in the example above, messages from `-1001234567890` go to `-1005555555555`, and messages from `-1009876543210` go to `-1006666666666`.

### Run it as a long-running container

For production, edit `docker-compose.yml` and uncomment the `command:` block with your specific IDs/hashes, then:

```bash
docker compose up -d
docker compose logs -f forwarder
```

---

## 6. Kill the user session

Revokes the session on Telegram's side and removes local session files:

```bash
docker compose run --rm forwarder logout
```

After this, the next `forward` will fail until you `login` again.

---

## Cloud deployment

The image is a single-process, stateful (because of session files) container. Anywhere you can run a container with a persistent volume works.

**AWS ECS (Fargate)**
- Push the image to ECR.
- Create a task definition with one container, env vars from Secrets Manager.
- Attach an EFS volume mounted at `/data/sessions`.
- For the one-time `login`, run the task with `command` overridden to `["login"]` from `aws ecs execute-command` or run it locally first against the EFS mount.

**GCP Cloud Run / GKE**
- Cloud Run jobs work for the long-running forwarder (set min instances = 1 if you don't want cold-stop), with a Filestore mount for sessions.
- GKE: use a `Deployment` with a `PersistentVolumeClaim` mounted at `/data/sessions`, env vars via `Secret` + `envFrom`.

**Anywhere else (single VM)**
```bash
docker compose up -d
```
The `./data/sessions` host directory is your persistent state — back it up if you care about not re-doing login.

---

## Local development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # fill values
export SESSION_DIR=./sessions   # override the default /data/sessions
python -m src.main login
python -m src.main list-groups
python -m src.main forward -gid -100... -gh ... -mid -100... -mh ...
```

---

## How it works (short version)

- **User client** (your phone-number-authenticated session) listens for `NewMessage` events on each source chat. Only a real user account can listen to arbitrary chats it's a member of.
- **Bot client** (the BotFather token) sends to each destination chat. Sending as the bot keeps your personal account out of the destination groups.
- **Per-pair message-id map.** When the user client sees a new message, the forwarder records the source-id and the destination-id returned by the bot's send. When a future message in the source replies to an earlier one, the forwarder looks up the corresponding destination id and sets `reply_to` so it shows as a reply in the destination too.
- **Media.** Downloaded as bytes via the user client (which has access), then re-uploaded via the bot. Sender hints (`voice_note=True`, `video_note=True`, filename attributes) are set so the destination renders the right widget.
- **Polls.** Telegram polls are bound to a sender, so the original `Poll` can't be re-sent verbatim. The forwarder reconstructs an equivalent poll (question, options, multiple-choice / quiz / public-voters / close behavior) and sends it via `InputMediaPoll`.

---

## Limitations

- Stickers are forwarded as `.webp` files; bots have restricted sticker-send capabilities and can't always reproduce a sticker exactly.
- Service messages (joins, pins, etc.) are skipped.
- Edits and deletions in the source aren't propagated — only `NewMessage` is handled.
- The bot must be a member of every destination chat (and have permission to post).

---

## Troubleshooting

- **"User session not authorized."** Run `login` first. If you've cleared `./data/sessions/`, you'll need to log in again.
- **`PEER_ID_INVALID` from the bot.** The bot isn't in that destination chat, or you have the wrong access_hash. Re-run `list-groups` from a session that's a member of the destination, or add the bot and re-check.
- **Login asks again every time.** Make sure `./data/sessions` is actually mounted into the container. `docker compose config` will show the resolved volumes.
- **2FA never prompted but you have 2FA on.** You're not running with `-it`. Use `docker compose run --rm forwarder login` (not `exec`, not background `up`).
