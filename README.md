# telegram-forwarder

Forward messages from **N Telegram chats** to **N mapped Telegram chats** (1:1) using a Telegram bot. Built with [Telethon](https://docs.telethon.dev/), packaged for Docker, ready to deploy on AWS / GCP / any container runtime.

It supports text, photos, videos, voice messages, video notes, GIFs, audio files, generic documents, polls — replies stay threaded, missed messages are caught up on restart, and forum-supergroup ("community") topics can be filtered on the source and routed on the destination.

---

## Features

- **Multiple user accounts.** Log in any number of Telegram user accounts. Each gets a deterministic 6-character `short_id` (hash of the user's Telegram id) that you reference in every other command.
- **N → N forwarding.** As many source → destination pairs as you want in a single process.
- **Topic-aware.** Filter source by forum topic; route destination into a specific topic.
- **Catch-up on restart.** The newest queued message id is kept per source chat. On startup, missed messages are queued, in order, before the live ones. First-ever run for a chat establishes a baseline at "now" — no full-history backfill.
- **Reply preservation.** Replies in source become replies in destination, via a per-pair message-id map kept on disk, so it survives restarts.
- **Telegram and WhatsApp to Telegram and Signal.** Any pair can post to Telegram or to Signal (§12, Signal destinations); one platform-neutral pipeline does the forwarding for every source.
- **Sender attribution (optional).** Prefix each forwarded message with the original sender's name and `@username` — useful when mirroring a chat where the destination audience won't otherwise know who said what.
- **Config-file driven.** Express your entire forwarding topology in JSON; mix explicit `pairs` with `auto` blocks that mirror a community by topic title.
- **Container-ready.** Persistent volume for sessions and state, env-var config, no host paths baked in.
- **WhatsApp bridge, to Telegram and Signal.** Link a WhatsApp account as a linked device and forward chosen WhatsApp groups or chats into Telegram chats/topics with the same bot, or into Signal groups/chats through signal-cli-rest-api. See [§12](#12-whatsapp-bridge-to-telegram-and-signal).

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
├── .githooks/                            # pre-commit / pre-push checks that keep private data out of git
├── README.md
├── deploy/                               # AWS deployment runbook + PowerShell and bash automation
│   ├── README.md
│   ├── deploy-aws.ps1 / deploy-aws.sh
│   ├── load-deployment.ps1 / load-deployment.sh
│   ├── teardown-aws.ps1 / teardown-aws.sh
│   └── setup-ec2.sh
├── configs/                              # bind-mounted to /app/configs
│   ├── README.md
│   ├── simple.example.json
│   ├── community-mirror.example.json
│   ├── mixed.example.json
│   └── whatsapp.example.json             # WhatsApp bridge config (§12)
├── src/
│   ├── main.py            # argparse CLI
│   ├── config.py          # env vars + paths
│   ├── auth.py            # login / logout / list-users
│   ├── users.py           # users.json registry, short_id derivation
│   ├── state.py           # legacy forward_state.json, read once to carry catch-up points over
│   ├── list_groups.py     # list-groups, list-topics, clone-topics
│   ├── core/              # platform-neutral: no platform library imported here
│   │   ├── pipeline.py    # queued messages -> every pair's destination (fan-out,
│   │   │                  #   replies, edits, retries, alerts)
│   │   ├── store.py       # on-disk queue, message map, sender names, catch-up points (SQLite)
│   │   ├── source.py      # the interface a source implements
│   │   ├── sink.py        # the interface a destination implements, and its errors
│   │   ├── model.py       # Message, Pair, Dest, Media, Poll
│   │   └── rich.py        # text + formatting spans; join / trim / split
│   └── adapters/          # one platform per package
│       ├── dests.py          # how configs name a destination (Telegram or Signal)
│       ├── telegram/
│       │   ├── source.py      # source: the user account; catch-up + live, through the queue
│       │   ├── sink.py        # destination: posts through the bot (Telethon)
│       │   ├── forward.py     # forward: source + sinks + pipeline
│       │   ├── config_file.py # configs/<short_id>.json and the -gid/-mid flags
│       │   ├── convert.py     # Telegram message -> what to post
│       │   ├── formatting.py  # Telegram entities <-> Rich text, every entity type
│       │   └── peers.py       # chat ids -> peers for the user or the bot
│       ├── signal/sink.py    # destination: posts through signal-cli-rest-api (HTTP)
│       └── whatsapp/         # source: WhatsApp chats -> Telegram and Signal (§12)
│           ├── source.py      # the linked device: session events, queuing, media
│           ├── bridge.py      # wa-forward: source + sinks + pipeline
│           ├── client.py      # the only module that imports neonize
│           ├── convert.py     # WhatsApp message -> what to post
│           ├── formatting.py  # WhatsApp *bold* etc. -> Rich text
│           ├── accounts.py    # wa_accounts.json registry, short ids
│           ├── config_file.py # configs/wa-<short_id>.json loader
│           └── link.py        # wa-link / wa-unlink / wa-list-*
└── tests/                 # unit tests (run inside the image, see §12)
```

The persistent volume `/data/sessions` (host: `./data/sessions`) holds:

```
users.json                      # registry of logged-in accounts
user_<short_id>.session         # per-user Telethon session
bot_session_<short_id>.session  # the bot's session, one per forwarder process
tg_state_<short_id>.sqlite      # per-user queue, message map and catch-up points (§10)
forward_state_<short_id>.json   # legacy catch-up points, read once when upgrading
wa_*                            # WhatsApp accounts, sessions and queues (§12)
```

---

## Setup

### 1. Configure

Once per clone, turn on the hooks that keep credentials and real configs out of git (see [Keeping private data out of git](#keeping-private-data-out-of-git)):

```bash
git config core.hooksPath .githooks
```

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
| `forward …` | The actual forwarder. Takes `-c <config.json>` (any source: Telegram or WhatsApp) or Telegram pairs as CLI flags; `--dry-run` logs instead of posting. |
| `wa-link [--phone +<number>]` | Link a WhatsApp account with a pairing code (§12). |
| `wa-list-accounts` | Show linked WhatsApp accounts with their `short_id`. |
| `wa-list-groups -a <short_id>` | Show a WhatsApp account's groups with their ids (`./forwarder.sh wa-groups` wraps it). |
| `wa-unlink -a <short_id>` | Log a WhatsApp account out and delete its files. |
| `wa-forward -c <config.json> [--dry-run]` | The same as `forward -c`, for a WhatsApp config (long-running). |

Every command except `forward` and `wa-forward` is a one-shot operation; those two are the long-running ones.

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
Logged in for Alex @alex_user (short_id=aaaa11, user_id=123456789).
```

**Save the `short_id`** — that's how you'll reference this account in every other command. Run `login` again to add more accounts.

```bash
docker compose run --rm forwarder list-users
```

```
SHORT_ID  USER_ID       PHONE             NAME                            SESSION_FILE
aaaa11    123456789     +14155551234      Alex @alex_user                 user_aaaa11.session
bbbb22    987654321     +919876543210     Another User @other_user        user_bbbb22.session
```

To revoke a user's session and delete their local files:

```bash
docker compose run --rm forwarder logout -u aaaa11
```

---

## 4. Discover groups

```bash
docker compose run --rm forwarder list-groups -u aaaa11
```

```
TYPE       CHAT_ID           ACCESS_HASH           TITLE
supergroup -1009999900003    2222222222222222222   My source group
group      -9999900001       0                     My basic source group
community  -1009999900001    3333333333333333333   Example Community
```

- **`supergroup`** — modern group, has an access_hash.
- **`group`** — legacy basic group, no access_hash. Pass `0` to `-gh` / `-mh` and the tool routes it through `InputPeerChat`.
- **`community`** — forum-enabled supergroup with topics. List topics with `list-topics`.

---

## 5. Communities (forum supergroups / topics)

Telegram "communities" are forum-enabled supergroups: a single supergroup containing multiple **topics** (threaded sub-channels). Topics share the parent's chat_id. Each topic is identified by the id of its first message ("top message id"), which is what the `topic` argument takes.

**List topics in a community:**

```bash
docker compose run --rm forwarder list-topics -u aaaa11 -gid -1009999900001
```

```
Topics in 'Example Community' (parent_chat_id=-1009999900001)
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
  -u aaaa11 \
  -gid -1009999900001 \
  -mid -1008888800001
```

After cloning, run `list-topics` on the destination to discover the new TOPIC_IDs (each forum has its own id space — the destination's "Stock picks" topic has a different id from the source's "Stock picks").

---

## 6. Forwarding — config file (recommended)

Every forwarder, whatever its source, is described by the same kind of JSON config: which account reads the messages (a Telegram user here, or a WhatsApp account, §12), and pairs that send each source chat to destinations on Telegram or Signal. Drop it in `./configs/` and pass it with `-c`:

```bash
docker compose run --rm forwarder forward -c /app/configs/mixed.json
```

The `./configs` directory is bind-mounted to `/app/configs` in the container by `docker-compose.yml`, so you can swap configs without rebuilding. Edits to the file are visible inside the container immediately; `docker compose restart forwarder` picks up changes.

### Schema

```json
{
  "platform": "telegram",
  "account": "aaaa11",

  "pairs": [
    {
      "source": "-1009999900003",
      "source_hash": 1111111111111111111,
      "dest":   "-1008888800002",
      "dest_hash": 0,
      "topic": 17,
      "dest_topic": 5
    },
    {
      "source": "-1009999900004",
      "source_hash": 3333333333333333333,
      "dests": [
        { "dest": "-1008888800003", "dest_hash": 0 },
        { "dest_platform": "signal", "dest": "group.YWJjZGVmZ2hpams=" }
      ]
    }
  ],

  "auto": [
    {
      "source": "-1009999900001",
      "source_hash": 2222222222222222222,
      "dest":   "-1008888800001",
      "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"],
      "exclude": ["General"]
    }
  ]
}
```

| Top-level key | Required? | Meaning |
|---|---|---|
| `platform` | recommended | Where messages come from: `"telegram"` or `"whatsapp"` (§12). Older files without it are recognized by their contents. |
| `account` | only if `-u` not on CLI | Whose session reads the sources: a short ID from `list-users` (Telegram) or `wa-list-accounts` (WhatsApp). `user` is the same thing in Telegram configs written before. |
| `pairs` | one of `pairs`/`auto` is required | List of explicit source → destination pairs. |
| `auto` | one of `pairs`/`auto` is required | List of community-mirror blocks (expand at startup). Telegram only. |
| `attribution` | optional, default `false` | File-level default for whether to prefix forwarded messages with the source sender's name. Each entry below can override. |
| `alerts` | optional | A Telegram user id the bot DMs when something needs attention (session logged out, a destination unreachable, a message given up). That user must press **Start** on the bot once. |

### `pairs` — explicit, one-by-one

Each entry is one source chat and where it goes: either one destination, given in the entry itself, or several, listed under `dests` (each item has the destination fields below, and may set its own `attribution`):

| Field | Required? | Meaning |
|---|---|---|
| `source` | yes | Source chat id (e.g. `-1009999900003` or `-9999900001` for basic groups). |
| `source_hash` | yes | Source access_hash. `0` for basic groups. |
| `dest` | yes | Destination chat id; for Signal, a group id or `+phone`. |
| `dest_hash` | Telegram only | Destination access_hash. `0` for basic groups; ignored at runtime since the bot resolves its own. |
| `topic` | optional, default `0` | Source forum topic id. `0` = no filter. |
| `dest_topic` | optional, default `0` | Destination forum topic id. `0` = main feed. Telegram only. |
| `dest_platform` | optional, default `"telegram"` | `"signal"` posts to Signal instead (§12, Signal destinations). |
| `dests` | instead of the `dest…` fields | A list of destinations for this source, e.g. one Telegram chat and one Signal group. |
| `attribution` | optional, inherits file default | Override the file-level setting for this pair (and the default for its `dests`). |

Configs written before this format, with `user` (Telegram) or `account` and WhatsApp sources, load unchanged and keep all their state: pairs are identified exactly as before.

### `auto` — community-mirror form

Each entry describes a *pair of communities*. At startup the forwarder reads both topic lists and emits one pair per topic title that exists in **both**:

| Field | Required? | Meaning |
|---|---|---|
| `source` / `source_hash` | yes | Source community. |
| `dest` / `dest_hash` | yes | Destination community. |
| `include` | optional | Whitelist of titles. If present, only these titles are considered. |
| `exclude` | optional | Blacklist of titles. Always applied after `include`. |
| `attribution` | optional, inherits file default | Override the file-level setting for every pair this block expands into. |

`auto` matches by title. If you rename a topic in only one community, that pair stops firing until both sides are renamed (or moved into `pairs` with explicit ids). For rock-solid mappings, use `pairs`. For convenience after `clone-topics`, use `auto`.

### Sender attribution

When `attribution` is `true`, every forwarded message is prefixed with a bolded sender header:

```
Alex Doe (@alex_doe):

The original message text follows here.
```

The sender's first name and last name are concatenated; the `@username` is appended in parentheses when present. Users with no first/last name fall back to just `@username`; users with neither show as `(unknown user)`. Anonymous channel posts use the channel title.

For media (photo, video, document, voice, etc.) the prefix is added to the caption. Polls have no text body, so they're forwarded unchanged regardless of this setting.

The `attribution` field can live at three places. The closest one wins:

1. **On a `pairs` entry** or an `auto` block — applies to that entry only.
2. **At the top level** of the config — applies as the default for every entry.
3. **Nowhere** — defaults to `false` (original behavior, no prefix).

This means you can set the file-level default to `true` and turn it off for specific pairs, or vice versa. See `configs/mixed.example.json` for a runnable example using both directions.

### Mixed example

You can use `pairs` and `auto` together. Topology: 2 specific topics from community A, all of community E except General, and three plain supergroups B/C/D forwarded as-is. Attribution is enabled at the file level and overridden off for one block:

```json
{
  "platform": "telegram",
  "account": "aaaa11",

  "attribution": true,

  "auto": [
    {
      "source": "-1001000000001", "source_hash": 1111111111111111111,
      "dest":   "-1002000000001", "dest_hash": 0,
      "include": ["Stock picks", "Macro & news"]
    },
    {
      "source": "-1001000000005", "source_hash": 5555555555555555555,
      "dest":   "-1002000000005", "dest_hash": 0,
      "exclude": ["General"],
      "attribution": false
    }
  ],

  "pairs": [
    {"source":"-1001000000002", "source_hash":2222222222222222222, "dest":"-1002000000002", "dest_hash":0},
    {"source":"-1001000000003", "source_hash":3333333333333333333, "dest":"-1002000000003", "dest_hash":0},
    {"source":"-1001000000004", "source_hash":4444444444444444444, "dest":"-1002000000004", "dest_hash":0}
  ]
}
```

This expands to 8 pairs at startup (2 from A's auto block + 3 from E's auto block + 3 explicit). See `configs/mixed.example.json` for the runnable template.

---

## 7. Forwarding — CLI flags (alternative)

Same thing, just without the config file. Useful for quick one-offs:

```bash
docker compose run --rm forwarder forward \
  -u aaaa11 \
  -gid -1009999900003 -1009999900002 \
  -gh  2222222222222222222 1111111111111111111 \
  -mid -1008888800001 -1008888800002 \
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
| `-attr` | `--attribution` | *Optional flag.* Prefix forwarded messages with the source sender's name and `@username`. Applies to all pairs in this invocation. For per-pair control, use a config file. |

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
docker compose run --rm forwarder login                    # prints short_id, e.g. aaaa11
nano configs/aaaa11.json                                   # define their pairs

# Start, stop, tail logs, list:
./forwarder.sh start aaaa11
./forwarder.sh logs  aaaa11                                # Ctrl+C exits the tail
./forwarder.sh ps                                          # show all forwarder containers
./forwarder.sh restart aaaa11
./forwarder.sh stop aaaa11

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
docker compose run --rm forwarder login                    # prints short_id, e.g. aaaa11
nano configs/aaaa11.json

# Add a service block to docker-compose.users.yml for each user, then:
docker compose -f docker-compose.users.yml up -d

# Day-to-day
docker compose -f docker-compose.users.yml ps
docker compose -f docker-compose.users.yml logs -f forwarder-aaaa11
docker compose -f docker-compose.users.yml restart forwarder-aaaa11
docker compose -f docker-compose.users.yml down
```

To avoid typing `-f docker-compose.users.yml` every time, add this to your shell:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.users.yml
```

After that, `docker compose up -d` brings everything up (including the multi-user services), and one-off commands like `docker compose run --rm forwarder login` keep working through the base file.

A new user is one block:

```yaml
  forwarder-bbbb22:
    <<: *forwarder
    container_name: tg-forwarder-bbbb22
    command: ["forward", "-c", "/app/configs/bbbb22.json"]
```

The `<<: *forwarder` line is a YAML anchor reference that expands the shared image/env_file/volumes/restart fields at file-load time. It's a pure YAML feature, not a compose feature. Then `docker compose -f docker-compose.users.yml up -d` — compose reconciles, starting the new one without touching the others.

Both options share `/data/sessions`, so `users.json` stays consistent and `login` only needs to happen once per user no matter which option you use.

**To run a single user long-term** (the basic case, no multi-user setup at all), edit the `command:` in `docker-compose.yml` to point at one config and `docker compose up -d`. The two options above are only relevant when you want several users running concurrently.

---

## 9. Cloud deployment

The image is a single-process container with stateful sessions on disk. Anywhere you can run a container with a persistent volume works.

**AWS EC2 (simplest, recommended for this project):** an end-to-end runbook plus automation lives in [`deploy/`](./deploy/README.md), as PowerShell scripts for Windows and identical bash scripts for Linux. They use the AWS CLI profile `tgf` by default. One script provisions a `t3.micro` with Docker pre-installed, saves the SSH key and deployment metadata to a folder you specify; a companion script reloads everything in a fresh shell and provides `Connect-Forwarder` / `Start-Forwarder` / `Stop-Forwarder` helpers.

```powershell
# Quick provision (full guide in deploy/README.md)
cd deploy
.\deploy-aws.ps1 -OutputFolder C:\Users\me\tg-forwarder-aws
```

```bash
# Same on Linux
deploy/deploy-aws.sh --output-folder ~/tg-forwarder-aws
```

**AWS ECS (Fargate):** push the image to ECR, attach an EFS volume mounted at `/data/sessions`. Don't run more than one task pointed at the same EFS — the SQLite session files only support a single writer.

**GCP Cloud Run / GKE:** Cloud Run with min-instances=1 and a Filestore mount, or GKE `Deployment` + `PersistentVolumeClaim` on `/data/sessions`. Env vars from a `Secret`.

---

## 10. How catch-up works

Every message from a source chat goes into an on-disk queue (`data/sessions/tg_state_<short_id>.sqlite`) before it's forwarded, and each source chat (or topic) has a catch-up point: the newest message id queued from it.

On startup, for every source chat:

1. **First-ever run** (no catch-up point): query the latest message in scope (the chat as a whole, or the topic if filtered) and record its id as the baseline. **No backfill.** Future runs only catch up missed messages from this point.
2. **Subsequent runs:** call `iter_messages(min_id=point, reply_to=topic_id_if_any)` and queue the results oldest first. The worker then forwards the queue in order and retries failures (5 s → 5 min), so a failed forward no longer drops the message.

The live listener is attached *before* catch-up starts, and what it sees meanwhile waits until catch-up has queued everything older, so nothing falls in between and the order is kept. A message is queued once even if catch-up and the listener both see it.

Upgrading from the old forwarder: on first start, each chat's catch-up point is taken from `forward_state_<short_id>.json` (the newest of that chat's pairs), so forwarding continues where it stopped. That file isn't written any more.

---

## 11. How it works (architecture)

- **One pipeline for every source** (`src/core/pipeline.py`). The Telegram source (`src/adapters/telegram/source.py`) and the WhatsApp source both put messages on an on-disk queue; one worker forwards it in order to each pair's destination through that platform's sink (the Telegram bot, or Signal). A destination that can't be reached is paused and rechecked every minute without holding up the others.
- **User client** (one of your phone-number-authenticated sessions, selected by `-u` or by the config's `user`) listens for `NewMessage` events on each source.
- **Bot client** (the BotFather token from `.env`) sends to each destination. The user account never appears in destination groups.
- **Per-account access hashes.** Telegram access_hashes are per-account. The user uses its own hash for source supergroups. The bot always re-resolves its destination via `get_entity` so it gets the bot's own hash — that's why `dest_hash` is informational only.
- **Topic filtering on source.** A message belongs to topic T if its `reply_to.reply_to_top_id == T` or `reply_to.reply_to_msg_id == T` (the topic's root message). Catch-up uses Telethon's `iter_messages(reply_to=T)`.
- **Topic routing on destination.** Posting into topic T means setting `reply_to=T` on the outgoing message. The forwarder uses `dest_topic_id` as the default `reply_to` for top-level messages. Replies to messages we've already forwarded use the mapped reply id instead (so threads stay intact within the topic).
- **Media handoff.** Photos/videos/documents/voice etc. are downloaded by the user client (which has access to the source) and re-uploaded by the bot (which has access to the destination), keeping duration, size, waveform, title and file name. Polls are reconstructed (the original `Poll` is bound to the source chat's poll id). If Telegram's file reference has expired by the time a queued message is sent, the message is fetched again.
- **Formatting** passes through unchanged: every entity type (bold, links, mentions, custom emoji, code languages, quotes and newer types) is carried as is.
- **Reply mapping** is kept per pair in the queue's SQLite file, so replies to messages forwarded before a restart still thread.

---

## 12. WhatsApp bridge (to Telegram and Signal)

Forwards chosen WhatsApp groups (or 1:1 chats) into Telegram chats or topics, posted by the same bot, and/or into Signal groups or chats (see [Signal destinations](#signal-destinations)). It is one-way and read-only toward WhatsApp: it never sends messages there, never marks chats as read and never sets a presence, so the phone keeps getting notifications.

**How it works.** The bridge links to the WhatsApp account as a *linked device*, like WhatsApp Web (WhatsApp allows up to 4). From then on WhatsApp sends it its own end-to-end-encrypted copy of every message. It uses [neonize](https://github.com/krypton-byte/neonize), a Python wrapper around the Go library [whatsmeow](https://github.com/tulir/whatsmeow). neonize is pinned exactly in `requirements.txt` and only `src/adapters/whatsapp/client.py` imports it. The WhatsApp side is just a *source*: forwarding itself (queue, fan-out, retries, replies, edits) is the platform-neutral pipeline in `src/core/`.

> **Unofficial client.** Using it is against WhatsApp's terms and the account could be banned. Read-only use rarely is, but nothing is guaranteed. To keep the risk off a main number, link a second number that has joined the source groups.

### Setup

```bash
# 1. Link (once per account). Prints an 8-character code; on the phone open
#    WhatsApp > Settings > Linked devices > Link a device >
#    "Link with phone number instead" and type it within ~2 minutes.
docker compose run --rm forwarder wa-link --phone +919876543210

# 2. Find group ids (pauses the account's bridge while listing, if running)
./forwarder.sh wa-groups <short_id>

# 3. Write configs/wa-<short_id>.json (see configs/whatsapp.example.json)

# 4. Optional: check what it would post, without posting
docker compose run --rm forwarder wa-forward -c /app/configs/wa-<short_id>.json --dry-run

# 5. Run it like any other forwarder
./forwarder.sh start wa-<short_id>
./forwarder.sh logs  wa-<short_id>
```

### Config

The same format as every other config (§6), with `"platform": "whatsapp"`:

```json
{
  "platform": "whatsapp",
  "account": "a1b2c3",
  "attribution": true,
  "alerts": 123456789,
  "pairs": [
    { "source": "120363012345678901@g.us",
      "dests": [ { "dest": "-1004200000001", "dest_hash": 1234567890123456789 },
                 { "dest_platform": "signal", "dest": "group.YWJjZGVmZ2hpams=" } ] },
    { "source": "+919876543210", "dest": "-1004200000002", "dest_hash": 987654321, "dest_topic": 12, "attribution": false }
  ]
}
```

- `source`: a group id from `wa-list-groups`, or a `+phone` for a 1:1 chat. WhatsApp Channels aren't supported yet.
- Destinations work as in §6: `dest`, `dest_hash`, `dest_topic` for Telegram, `"dest_platform": "signal"` for Signal (a group id or `+phone`, no `dest_hash`/`dest_topic`), and `dests` for several.
- `attribution`: prefix each message with the sender's WhatsApp profile name. Phone numbers are never shown; unknown @mentions become `@someone`.
- `alerts` (optional): a Telegram user id the bot DMs when something needs attention (logged out, banned, client outdated, a message given up). That user must press **Start** on the bot once.

### What gets forwarded

| WhatsApp | Telegram |
|---|---|
| Text (`*bold*`, `_italic_`, `~strike~`, `` `code` ``, ```` ``` ````, `> quote`) | Text with real Telegram formatting; long texts are split |
| Photo, video, GIF, round video | Photo, video (with duration), animation |
| Voice note, audio, document | Voice note (with waveform), audio, file with its original name |
| Sticker | Sticker; animated ones as an animation |
| Poll | Telegram poll (as a numbered list if Telegram's limits don't fit) |
| Location, contact, event, group invite | Text (map link, contact details, time; invite links are left out) |
| Reply | Threaded reply if the original was forwarded, else a quote block |
| Edit | The Telegram copy is edited |

Not forwarded: view-once media (WhatsApp doesn't give it to linked devices), reactions, deletions, calls, status updates and Lottie stickers. Albums arrive as separate photos.

### Signal destinations

Signal pairs post through [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api), which runs in its own container with a Signal account linked to it, the way Signal Desktop is linked to a phone. Messages appear as sent by that account.

```bash
# 1. Start it. Its data goes to ./data/signal; its port is open on this machine only.
docker compose --profile signal up -d signal-api

# 2. Link a Signal account: open this page on the same machine (from elsewhere:
#    ssh -L 8080:localhost:8080 <server>) and scan the QR code with
#    Signal > Settings > Linked devices > Link new device.
#    http://localhost:8080/v1/qrcodelink?device_name=telegram-forwarder

# 3. Put that account's number in .env
SIGNAL_SENDER=+15551234567

# 4. Find group ids: the "id" field (group.…) of the groups the account is in
curl -s http://localhost:8080/v1/groups/+15551234567

# 5. Add pairs with "dest_platform": "signal", then (re)start the bridge. Start it
#    after signal-api, so it joins the telegram-forwarder network that reaches it.
./forwarder.sh restart wa-<short_id>
```

| WhatsApp | Signal |
|---|---|
| Text | Plain text: quote blocks become `> ` lines, other formatting is dropped; split at 2000 characters |
| Photo, video, GIF, voice note, audio, document, sticker | An attachment with its caption (voice notes as audio files, GIFs as videos, stickers as images). Over 100 MB, the caption with a note |
| Poll | A numbered list |
| Reply | A Signal quote if the original was forwarded there, else a quote block |
| Edit | The Signal copy is edited (text messages; captions stay as sent) |

- If signal-api is down, the account isn't linked or a recipient is wrong, those Signal pairs pause, with a log line and an alert. They're checked every minute; Telegram pairs keep going meanwhile, and messages that arrive while a pair is paused are skipped for it.
- `data/signal` holds the linked account's keys: whoever has it can send as that account. Treat it like the session files.
- Keep signal-api running: Signal unlinks devices that stay inactive for 30 days.

### Reliability

- Each incoming message is written to a SQLite queue (`data/sessions/wa_state_<short_id>.sqlite`) before anything else. WhatsApp considers a message delivered once the device receives it, so this queue is what makes a crash or restart lose nothing. One worker forwards in arrival order.
- While the bridge is down, WhatsApp holds messages for the linked device and delivers them when it reconnects. The chat history WhatsApp sends right after linking is ignored, so forwarding starts from the moment of linking.
- Failed sends are retried (5 s → 5 min); rate limits are waited out. A message a platform rejects outright (e.g. the bot was removed) is skipped and alerted. One destination's trouble doesn't hold up the others: each message goes to every reachable destination before a failed one is retried, and pairs already done are skipped on the retry. Media that WhatsApp no longer serves is replaced by a note.
- If WhatsApp logs the device out, bans the account or calls the client outdated, the bridge alerts and idles instead of crash-looping. Fix the cause, then `./forwarder.sh restart wa-<short_id>`.

### Upkeep

- Open WhatsApp on the phone at least every 14 days, or WhatsApp unlinks all linked devices.
- `data/sessions/wa_<short_id>.db` is the WhatsApp session. Whoever has it can read the account's messages, so treat it like the Telegram session files.
- If WhatsApp changes its protocol (alert: "client is outdated"), bump the neonize pin, rebuild, run the tests, restart.
- Run only one process per WhatsApp session. A second connection (e.g. `wa-list-groups` while the bridge runs) kicks the first off; `./forwarder.sh wa-groups` pauses the bridge for you.

### Tests

```bash
docker run --rm -v "$PWD:/app" --entrypoint python telegram-forwarder:latest \
    -m unittest discover -s tests -t . -v
```

---

## Keeping private data out of git

Private here means: `.env` (API keys, bot token, Signal number), your real configs (`configs/*.json`, which carry chat ids and access hashes), and `data/` (sessions and queues; whoever has a session file can use that account). Two layers keep them out of commits:

- **`.gitignore`** ignores every env file except `.env.example`, everything in `configs/` except the README and `*.example.json`, `data/`, sessions and SQLite files.
- **Git hooks** in `.githooks/` catch what an ignore list can't: a file force-added with `git add -f`, and private *values* pasted into a tracked file (a real chat id used as an example, a token in a test). Turn them on once per clone:

  ```bash
  git config core.hooksPath .githooks
  ```

  Before each commit and each push they check every file for private file paths, credential-shaped text (bot tokens, API hashes, private keys), and any value found in your real configs and env files, then refuse and say which file and what kind of value, never the value itself. To check by hand: `python3 .githooks/check_private.py --tracked` (this checkout) or `--history` (every commit). `--no-verify` skips a hook, so use it only for a false alarm.

Keep example configs and docs on made-up ids like the ones in `configs/*.example.json`. If something private does get pushed, treat it as public: revoke the bot token with @BotFather (`/revoke`), make a new API app at my.telegram.org, end sessions under Telegram > Settings > Devices, unlink WhatsApp and Signal devices, then rewrite the history that carries it.

---

## Limitations

- Static stickers arrive as stickers (without their emoji); animated ones as files.
- Service messages (joins, pins, etc.), dice and games are skipped.
- Edits and deletions in a Telegram source aren't propagated — only `NewMessage` is handled.
- Poll extras newer than quizzes (shuffled or added answers, hidden results) and media spoilers aren't copied.
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
- **WhatsApp: `wa-link` code expired** — `wa-link` makes up to 3 codes, about 2 minutes each. Have the phone on the "Link with phone number instead" screen before starting it.
- **WhatsApp: nothing arrives from a group** — the bridge log says on startup whether the account is a member of each source group. Check it with `./forwarder.sh logs wa-<short_id>`.
- **WhatsApp: "another program connected with this WhatsApp session"** — two processes used the same session, e.g. `wa-list-groups` while the bridge was running. The bridge restarts itself after 60 s.
