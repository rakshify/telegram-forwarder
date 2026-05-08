# telegram-forwarder

Forward messages from **N Telegram chats** to **N mapped Telegram chats** (1:1) using a Telegram bot. Built with [Telethon](https://docs.telethon.dev/), packaged for Docker, ready to deploy on AWS / GCP / any container runtime.

It supports text, photos, videos, voice messages, video notes, GIFs, audio files, generic documents, polls — replies stay threaded, missed messages are caught up on restart, and forum-supergroup ("community") topics can be filtered on the source and routed on the destination.

---

## Features

- **Multiple user accounts.** Log in any number of Telegram user accounts. Each gets a deterministic 6-character `short_id` (hash of the user's Telegram id) that you reference in every other command.
- **N → N forwarding.** As many source → destination pairs as you want in a single process.
- **Topic-aware.** Filter source by forum topic; route destination into a specific topic.
- **Catch-up on restart.** Last-forwarded source message id is persisted per pair. On startup, missed messages are backfilled before the live listener attaches. First-ever run for a pair establishes a baseline at "now" — no full-history backfill.
- **Reply preservation.** Replies in source become replies in destination, via a per-pair message-id map.
- **Config-file driven.** Express your entire forwarding topology in JSON; mix explicit `pairs` with `auto` blocks that mirror a community by topic title.
- **Container-ready.** Persistent volume for sessions and state, env-var config, no host paths baked in.

---

## Project layout

```
telegram-forwarder/
├── Dockerfile
├── docker-compose.yml                    # base service: build, one-off ops, single-user run
├── docker-compose.users.yml              # multi-user, declarative (Option B in §8)
├── entrypoint.sh
├── forwarder.sh                          # multi-user, dynamic / ad-hoc (Option A in §8)
├── requirements.txt
├── .env.example
├── README.md
├── deploy/                               # AWS deployment runbook + PowerShell automation
│   ├── README.md
│   ├── deploy-aws.ps1
│   ├── load-deployment.ps1
│   └── teardown-aws.ps1
├── configs/                              # bind-mounted to /app/configs
│   ├── README.md
│   ├── simple.example.json
│   ├── community-mirror.example.json
│   └── mixed.example.json
└── src/
    ├── main.py            # argparse CLI
    ├── config.py          # env vars + paths
    ├── auth.py            # login / logout / list-users
    ├── users.py           # users.json registry, short_id derivation
    ├── state.py           # forward_state.json (last-seen msg id per pair)
    ├── list_groups.py     # list-groups, list-topics, clone-topics
    ├── forwarder.py       # forward + catch-up logic
    └── config_file.py     # JSON config loader (-c / --config)
```

The persistent volume `/data/sessions` (host: `./data/sessions`) holds:

```
users.json                  # registry of logged-in accounts
forward_state.json          # last-forwarded msg id per (user, source, dest, topic)
user_<short_id>.session     # per-user Telethon session
bot_session.session         # bot session (single bot, shared)
```

---

## Setup

### 1. Configure

Get your API credentials at <https://my.telegram.org/apps>. Create a bot via [@BotFather](https://t.me/BotFather).

```bash
cp .env.example .env
# edit .env and fill in: TG_API_ID, TG_API_HASH, BOT_TOKEN
```

The bot must be a member of every destination chat with permission to send messages. For posting into specific topics, the bot also needs **Manage Topics** admin permission on the destination supergroup.

### 2. Build

```bash
docker compose build
```

---

## Commands at a glance

| Command | Purpose |
|---|---|
| `login` | Add a user account (interactive: phone, OTP, optional 2FA). |
| `list-users` | Show all logged-in accounts with `short_id`, name, phone. |
| `logout -u <short_id>` | Revoke a user's session and delete their local files. |
| `list-groups -u <short_id>` | Show every group/channel a user is in (id + access_hash). |
| `list-topics -u <short_id> -gid <community>` | Show topics inside a forum supergroup. |
| `clone-topics -u <short_id> -gid <src> -mid <dst>` | Copy topic structure from src community to dst community. |
| `forward …` | The actual forwarder. Takes either CLI flags or `-c <config.json>`. |

Every command except `forward` is a one-shot operation; `forward` is the long-running one.

---

## 3. Login

The login flow needs a TTY. Use `docker compose run` (not `up`):

```bash
docker compose run --rm forwarder login
```

You'll be prompted for:
1. Phone number with country code (e.g. `+14155551234`)
2. The OTP that Telegram sends you
3. Your 2FA password — only if you have 2FA enabled

After success it prints something like:

```
Logged in for Rakshit @rakshify (short_id=15e2b0, user_id=123456789).
```

**Save the `short_id`** — that's how you'll reference this account in every other command. Run `login` again to add more accounts.

```bash
docker compose run --rm forwarder list-users
```

```
SHORT_ID  USER_ID       PHONE             NAME                            SESSION_FILE
15e2b0    123456789     +14155551234      Rakshit @rakshify               user_15e2b0.session
8a9bcf    987654321     +919876543210     Other Account @other            user_8a9bcf.session
```

To revoke a user's session and delete their local files:

```bash
docker compose run --rm forwarder logout -u 15e2b0
```

---

## 4. Discover groups

```bash
docker compose run --rm forwarder list-groups -u 15e2b0
```

```
TYPE       CHAT_ID           ACCESS_HASH           TITLE
supergroup -1001234567890    1234567890123456789   My source group
group      -9876543210       0                     My basic source group
community  -1001969809629    7777777777777777777   Hyderabad Investing Enthusiasts
```

- **`supergroup`** — modern group, has an access_hash.
- **`group`** — legacy basic group, no access_hash. Pass `0` to `-gh` / `-mh` and the tool routes it through `InputPeerChat`.
- **`community`** — forum-enabled supergroup with topics. List topics with `list-topics`.

---

## 5. Communities (forum supergroups / topics)

Telegram "communities" are forum-enabled supergroups: a single supergroup containing multiple **topics** (threaded sub-channels). Topics share the parent's chat_id. Each topic is identified by the id of its first message ("top message id"), which is what the `topic` argument takes.

**List topics in a community:**

```bash
docker compose run --rm forwarder list-topics -u 15e2b0 -gid -1001969809629
```

```
Topics in 'Hyderabad Investing Enthusiasts' (parent_chat_id=-1001969809629)
TOPIC_ID    CLOSED  TITLE
1                   General
17                  Stock picks
42                  Macro & news
99          yes     Old retired threads
```

Topic id `1` is the always-present **General** topic.

**Clone a community's topic structure** into your own community. Both must already be forum-enabled supergroups (toggle "Topics" in group settings); the user account must be admin in the destination with manage-topics permission. Existing matching titles in the destination are skipped — idempotent.

```bash
docker compose run --rm forwarder clone-topics \
  -u 15e2b0 \
  -gid -1001969809629 \
  -mid -1005555555555
```

After cloning, run `list-topics` on the destination to discover the new TOPIC_IDs (each forum has its own id space — the destination's "Stock picks" topic has a different id from the source's "Stock picks").

---

## 6. Forwarding — config file (recommended)

For anything beyond a couple of pairs, drop a JSON config in `./configs/` and pass it with `-c`:

```bash
docker compose run --rm forwarder forward -c /app/configs/mixed.json
```

The `./configs` directory is bind-mounted to `/app/configs` in the container by `docker-compose.yml`, so you can swap configs without rebuilding. Edits to the file are visible inside the container immediately; `docker compose restart forwarder` picks up changes.

### Schema

```json
{
  "user": "15e2b0",

  "pairs": [
    {
      "source": "-1001234567890",
      "source_hash": 9876543210987654321,
      "dest":   "-1006666666666",
      "dest_hash": 0,
      "topic": 17,
      "dest_topic": 5
    }
  ],

  "auto": [
    {
      "source": "-1001969809629",
      "source_hash": 1234567890123456789,
      "dest":   "-1005555555555",
      "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"],
      "exclude": ["General"]
    }
  ]
}
```

| Top-level key | Required? | Meaning |
|---|---|---|
| `user` | only if `-u` not on CLI | Short ID from `list-users`. |
| `pairs` | one of `pairs`/`auto` is required | List of explicit ChatPair objects. |
| `auto` | one of `pairs`/`auto` is required | List of community-mirror blocks (expand at startup). |

### `pairs` — explicit, one-by-one

Each entry is exactly one ChatPair:

| Field | Required? | Meaning |
|---|---|---|
| `source` | yes | Source chat id (e.g. `-1001234567890` or `-9876543210` for basic groups). |
| `source_hash` | yes | Source access_hash. `0` for basic groups. |
| `dest` | yes | Destination chat id. |
| `dest_hash` | yes | Destination access_hash. `0` for basic groups; ignored at runtime since the bot resolves its own. |
| `topic` | optional, default `0` | Source forum topic id. `0` = no filter. |
| `dest_topic` | optional, default `0` | Destination forum topic id. `0` = main feed. |

### `auto` — community-mirror form

Each entry describes a *pair of communities*. At startup the forwarder reads both topic lists and emits one ChatPair per topic title that exists in **both**:

| Field | Required? | Meaning |
|---|---|---|
| `source` / `source_hash` | yes | Source community. |
| `dest` / `dest_hash` | yes | Destination community. |
| `include` | optional | Whitelist of titles. If present, only these titles are considered. |
| `exclude` | optional | Blacklist of titles. Always applied after `include`. |

`auto` matches by title. If you rename a topic in only one community, that pair stops firing until both sides are renamed (or moved into `pairs` with explicit ids). For rock-solid mappings, use `pairs`. For convenience after `clone-topics`, use `auto`.

### Mixed example

You can use `pairs` and `auto` together. Topology: 2 specific topics from community A, all of community E except General, and three plain supergroups B/C/D forwarded as-is:

```json
{
  "user": "15e2b0",

  "auto": [
    {
      "source": "-1001000000001", "source_hash": 1111111111111111111,
      "dest":   "-1002000000001", "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"]
    },
    {
      "source": "-1001000000005", "source_hash": 5555555555555555555,
      "dest":   "-1002000000005", "dest_hash": 0,
      "exclude": ["General"]
    }
  ],

  "pairs": [
    {"source":"-1001000000002", "source_hash":2222222222222222222, "dest":"-1002000000002", "dest_hash":0},
    {"source":"-1001000000003", "source_hash":3333333333333333333, "dest":"-1002000000003", "dest_hash":0},
    {"source":"-1001000000004", "source_hash":4444444444444444444, "dest":"-1002000000004", "dest_hash":0}
  ]
}
```

This expands to 8 ChatPairs at startup (2 from A's auto block + 3 from E's auto block + 3 explicit). See `configs/mixed.example.json` for the runnable template.

---

## 7. Forwarding — CLI flags (alternative)

Same thing, just without the config file. Useful for quick one-offs:

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
| `-u`   | `--user` | Short ID of the user account to listen with. Required unless `-c` is used and config has `user`. |
| `-c`   | `--config` | Path to a JSON config file (alternative to all the per-pair flags). |
| `-gid` | `--group_chat_id` | Source chat IDs (space-separated). |
| `-gh`  | `--group_chat_hash` | Source access_hashes, **same order as `-gid`**. `0` for basic groups. |
| `-mid` | `--mapped_chat_id` | Destination chat IDs, **1:1 mapped to `-gid` by position**. |
| `-mh`  | `--mapped_chat_hash` | Destination access_hashes (kept for symmetry; bot resolves its own at runtime). |
| `-tid` | `--topic_id` | *Optional.* Per-pair source topic ids inside a forum supergroup (1:1 with `-gid`). `0` = no filter. Once you pass `-tid` at all, every position needs a value (even `0`). |
| `-mtid` | `--mapped_topic_id` | *Optional.* Per-pair destination topic ids (1:1 with `-mid`). `0` = main feed. Same all-or-nothing rule as `-tid`. |

**Pairs are positional 1:1.** Position N across every list is one independent pair. Two `-gid`s with the same value are perfectly valid — they're two separate pairs that happen to share a source. So if community A has 2 topics you want, plain group B/C/D need no topics, and community E has 3 topics:

```bash
-gid <A> <A> <B> <C> <D> <E> <E> <E>
-gh  <Ah> <Ah> <Bh> <Ch> <Dh> <Eh> <Eh> <Eh>
-mid <dA> <dA> <dB> <dC> <dD> <dE> <dE> <dE>
-mh  0 0 0 0 0 0 0 0
-tid  <A_t1>  <A_t2>  0 0 0 <E_t1>  <E_t2>  <E_t3>
-mtid <dA_t1> <dA_t2> 0 0 0 <dE_t1> <dE_t2> <dE_t3>
```

For non-topic positions you put `0` in `-tid` and `-mtid` to keep the lists the same length. This gets unwieldy fast — section 6 (config file) is much easier.

---

## 8. Run as a long-running service

Edit `docker-compose.yml` and replace `command: ["--help"]` with your real invocation. Using a config file:

```yaml
    command: ["forward", "-c", "/app/configs/mixed.json"]
```

Then:

```bash
docker compose up -d
docker compose logs -f forwarder      # tail
```

The container survives:
- container crashes (`restart: unless-stopped`)
- EC2 / host reboots (Docker is `enable --now`'d on the host)
- your SSH session ending (it's detached)

To change the forwarding topology later: edit the config in `./configs/`, then `docker compose restart forwarder`. No rebuild needed.

### Multiple forwarders side by side — two options

Two patterns ship in the repo. Pick whichever fits how you work; you can switch later.

| | `forwarder.sh` (dynamic) | `docker-compose.users.yml` (static) |
|---|---|---|
| Where state lives | "Whatever containers happen to be running" | The compose file |
| Add a user | Drop `configs/<id>.json`, run `start <id>` | Edit `docker-compose.users.yml`, append a service block, `up -d` |
| Remove a user | `stop <id>` | Comment out the service, `up -d` |
| Single source of truth | No | Yes |
| Survives EC2 reboot | Yes (each container has `--restart unless-stopped`) | Yes (same) |
| Reconciles on `up -d` | N/A | Yes — extra services get started, removed services get stopped |
| Plays well with infra-as-code | No | Yes |
| Best when | Iterating, shell-driven workflows, ad-hoc users | Production-ish, multi-user EC2, anything you want versioned |

For an EC2 deployment that's meant to run unattended, **the static compose file is the right default.** The dynamic script is fine for local iteration.

#### Option A — `forwarder.sh` (dynamic, ad-hoc)

For more than one or two users, `forwarder.sh` runs one container per user from the same image compose builds. No per-user config in `docker-compose.yml`.

**Convention:** name each user's config `configs/<short_id>.json`. The script keys off that.

```bash
# One-time: build the image
docker compose build

# One-time per user: log in, then save their config
docker compose run --rm forwarder login                    # prints short_id, e.g. 15e2b0
nano configs/15e2b0.json                                   # define their pairs

# Start, stop, tail logs, list:
./forwarder.sh start 15e2b0
./forwarder.sh logs  15e2b0                                # Ctrl+C exits the tail
./forwarder.sh ps                                          # show all forwarder containers
./forwarder.sh restart 15e2b0
./forwarder.sh stop 15e2b0

# Bulk operations
./forwarder.sh start-all                                   # starts every configs/<id>.json
./forwarder.sh stop-all
```

Each container is named `tg-forwarder-<short_id>`, has `--restart unless-stopped`, and shares the `/data/sessions` volume.

#### Option B — `docker-compose.users.yml` (static, declarative)

`docker-compose.users.yml` ships in the repo. It uses a YAML anchor to define the shared service configuration once, then expands it into per-user services. Each user is a service; you add users by appending a 4-line block.

```bash
# One-time: build the image (compose still owns the build)
docker compose build

# One-time per user: log in
docker compose run --rm forwarder login                    # prints short_id, e.g. 15e2b0
nano configs/15e2b0.json

# Add a service block to docker-compose.users.yml for each user, then:
docker compose -f docker-compose.users.yml up -d

# Day-to-day
docker compose -f docker-compose.users.yml ps
docker compose -f docker-compose.users.yml logs -f forwarder-15e2b0
docker compose -f docker-compose.users.yml restart forwarder-15e2b0
docker compose -f docker-compose.users.yml down
```

To avoid typing `-f docker-compose.users.yml` every time, add this to your shell:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.users.yml
```

After that, `docker compose up -d` brings everything up (including the multi-user services), and one-off commands like `docker compose run --rm forwarder login` keep working through the base file.

A new user is one block:

```yaml
  forwarder-8a9bcf:
    <<: *forwarder
    container_name: tg-forwarder-8a9bcf
    command: ["forward", "-c", "/app/configs/8a9bcf.json"]
```

The `<<: *forwarder` line is a YAML anchor reference that expands the shared image/env_file/volumes/restart fields at file-load time. It's a pure YAML feature, not a compose feature. Then `docker compose -f docker-compose.users.yml up -d` — compose reconciles, starting the new one without touching the others.

Both options share `/data/sessions`, so `users.json` stays consistent and `login` only needs to happen once per user no matter which option you use.

**To run a single user long-term** (the basic case, no multi-user setup at all), edit the `command:` in `docker-compose.yml` to point at one config and `docker compose up -d`. The two options above are only relevant when you want several users running concurrently.

---

## 9. Cloud deployment

The image is a single-process container with stateful sessions on disk. Anywhere you can run a container with a persistent volume works.

**AWS EC2 (simplest, recommended for this project):** an end-to-end Windows-driven runbook plus PowerShell automation lives in [`deploy/`](./deploy/README.md). One script provisions a `t3.micro` with Docker pre-installed, saves the SSH key and deployment metadata to a folder you specify; a companion script reloads everything in a fresh shell and provides `Connect-Forwarder` / `Start-Forwarder` / `Stop-Forwarder` helpers.

```powershell
# Quick provision (full guide in deploy/README.md)
cd deploy
.\deploy-aws.ps1 -OutputFolder C:\Users\you\tg-forwarder-aws
```

**AWS ECS (Fargate):** push the image to ECR, attach an EFS volume mounted at `/data/sessions`. Don't run more than one task pointed at the same EFS — the SQLite session files only support a single writer.

**GCP Cloud Run / GKE:** Cloud Run with min-instances=1 and a Filestore mount, or GKE `Deployment` + `PersistentVolumeClaim` on `/data/sessions`. Env vars from a `Secret`.

---

## 10. How catch-up works

Every successfully forwarded message advances a per-pair counter in `forward_state.json`:

```
forward_state["<short_id>:<source_id>:<dest_id>"]            = last_msg_id   # non-topic pair
forward_state["<short_id>:<source_id>:<dest_id>:<topic_id>"] = last_msg_id   # topic-filtered pair
```

On startup, for every pair:

1. **First-ever run** (no entry in state): query the latest message in scope (the chat as a whole, or the topic if filtered), record its id as the baseline. **No backfill.** Future runs only catch up missed messages from this point.
2. **Subsequent runs:** call `iter_messages(min_id=last_seen, reply_to=topic_id_if_any)`, reverse to chronological order, forward each via the bot. State is saved after every successful forward. If a forward fails mid-catch-up, state is *not* advanced past the failed message — the next run retries from the same point.

Then the live listener attaches. Any incoming message with `id <= state[key]` is dropped (covers the brief overlap between catch-up finishing and the listener starting).

---

## 11. How it works (architecture)

- **User client** (one of your phone-number-authenticated sessions, selected by `-u` or by the config's `user`) listens for `NewMessage` events on each source.
- **Bot client** (the BotFather token from `.env`) sends to each destination. The user account never appears in destination groups.
- **Per-account access hashes.** Telegram access_hashes are per-account. The user uses its own hash for source supergroups. The bot always re-resolves its destination via `get_entity` so it gets the bot's own hash — that's why `dest_hash` is informational only.
- **Topic filtering on source.** A message belongs to topic T if its `reply_to.reply_to_top_id == T` or `reply_to.reply_to_msg_id == T` (the topic's root message). Catch-up uses Telethon's `iter_messages(reply_to=T)`.
- **Topic routing on destination.** Posting into topic T means setting `reply_to=T` on the outgoing message. The forwarder uses `dest_topic_id` as the default `reply_to` for top-level messages. Replies to messages we've already forwarded use the mapped reply id instead (so threads stay intact within the topic).
- **Media handoff.** Photos/videos/documents/voice etc. are downloaded by the user client (which has access to the source) and re-uploaded by the bot (which has access to the destination). Polls are reconstructed (the original `Poll` is bound to the source chat's poll id).
- **Reply mapping** is in-memory (per-pair `msg_id_map`). Replies to messages from a previous process lifetime aren't linked — they send as standalone messages (or land in the configured destination topic).

---

## Limitations

- Stickers are forwarded as `.webp` files; bots have restricted sticker-send capabilities.
- Service messages (joins, pins, etc.) are skipped.
- Edits and deletions in the source aren't propagated — only `NewMessage` is handled.
- Reply mapping is in-memory; replies in live messages that point to messages from a previous process lifetime aren't linked.
- The bot must be a member of every destination chat with permission to post (and **Manage Topics** if you're posting into specific topics).

---

## Troubleshooting

- **`No user with short id '…'`** — run `list-users` to see registered users; `login` if needed.
- **`User <id> session not authorized`** — session was revoked or deleted. Run `login` again for that account.
- **`Bot could not resolve dest chat`** — the bot isn't in the destination, or doesn't have permission. Add it manually first.
- **Login asks again every time** — make sure `./data/sessions` is mounted into the container. `docker compose config` shows resolved volumes.
- **2FA never prompted but you have 2FA on** — you're not running with `-it`. Use `docker compose run --rm forwarder login` (not `exec`, not background `up`).
- **`compose build requires buildx 0.17.0 or later`** — install Docker buildx as a CLI plugin on the host: `mkdir -p /usr/local/lib/docker/cli-plugins && curl -SL "https://github.com/docker/buildx/releases/latest/download/buildx-$(curl -s https://api.github.com/repos/docker/buildx/releases/latest | grep tag_name | cut -d\\\" -f4).linux-amd64" -o /usr/local/lib/docker/cli-plugins/docker-buildx && chmod +x /usr/local/lib/docker/cli-plugins/docker-buildx`.
- **`ImportError: cannot import name 'GetForumTopicsRequest' from 'telethon.tl.functions.channels'`** — your Telethon build moved this RPC to the `messages` module. The code already falls back automatically; if you still see this error, upgrade Telethon: `pip install -U telethon` (or rebuild the image).
- **`auto` block resolves to fewer pairs than expected** — title mismatch between source and destination. Run `list-topics` on both and compare; rename, or use `pairs` with explicit ids.
