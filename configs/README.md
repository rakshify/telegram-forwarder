# Forward configs

Drop one or more `*.json` config files in this directory. Each describes a set
of forwarding pairs for one source account: a Telegram user (`list-users`) or
a linked WhatsApp account (`wa-list-accounts`). Every config has the same
shape: `platform`, `account`, then `pairs` whose destinations can be Telegram
or Signal (README section 6). Configs written before this format still load.

The directory is bind-mounted into the container at `/app/configs` (see
`docker-compose.yml`), so you reference files from inside the container at
`/app/configs/<name>.json`:

    docker compose run --rm forwarder forward -c /app/configs/mixed.json

Edits to files in this folder are visible to the container immediately. To
make a running forwarder pick up changes:

    docker compose restart forwarder

The three `*.example.json` files in this directory are templates — copy and
edit, don't run them as-is (the ids are placeholders):

  - `simple.example.json`           one plain supergroup -> supergroup pair
  - `community-mirror.example.json` mirror a whole community into another
  - `mixed.example.json`            combine auto + pairs in one config
  - `whatsapp.example.json`         WhatsApp bridge; save as `wa-<short_id>.json`

See the project README, sections 6 and 7, for the full schema, and
section 12 for the WhatsApp bridge and for Signal destinations
(`"dest_platform": "signal"`, in any config).
