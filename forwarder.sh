#!/usr/bin/env bash
#
# forwarder.sh — dynamic per-user lifecycle helper.
#
# Compose handles the image, the shared volumes, and one-off commands.
# This script handles starting/stopping a long-running forwarder per user,
# without needing a separate service definition for each.
#
# Usage:
#   ./forwarder.sh start <short_id>      # start forwarder for one user
#   ./forwarder.sh start-all             # start all users with a configs/<id>.json
#   ./forwarder.sh stop  <short_id>
#   ./forwarder.sh stop-all
#   ./forwarder.sh restart <short_id>
#   ./forwarder.sh logs  <short_id>      # tail logs (Ctrl+C to stop tailing)
#   ./forwarder.sh ps                    # show all forwarder containers
#   ./forwarder.sh status <short_id>     # show one user's container status
#
# Convention: the config for user <short_id> lives at configs/<short_id>.json.
# That is what makes "100s of users" tractable — name your configs after the
# short_id and this script does the rest.
#
# Requirements:
#   - Docker installed
#   - The `telegram-forwarder:latest` image built (run `docker compose build`)
#   - A `.env` file in the project root with TG_API_ID, TG_API_HASH, BOT_TOKEN
#   - You've run `login` for each user (via `docker compose run --rm forwarder login`)
#   - configs/<short_id>.json exists for each user you want to forward for

set -euo pipefail

IMAGE="telegram-forwarder:latest"
CONTAINER_PREFIX="tg-forwarder"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# All forwarder containers carry this label so `ps` / `stop-all` can find them
# regardless of which user they're running.
LABEL_KEY="com.telegram-forwarder.role"
LABEL_VALUE="forwarder"

container_name() { echo "${CONTAINER_PREFIX}-$1"; }

require_image() {
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Error: image '$IMAGE' not found. Build it first:" >&2
    echo "  docker compose build" >&2
    exit 1
  fi
}

require_config() {
  local short_id="$1"
  local cfg="$PROJECT_DIR/configs/${short_id}.json"
  if [[ ! -f "$cfg" ]]; then
    echo "Error: config not found at $cfg" >&2
    echo "Convention: each user's config is configs/<short_id>.json" >&2
    exit 1
  fi
}

require_env() {
  if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Error: $PROJECT_DIR/.env not found." >&2
    echo "Copy .env.example and fill it in." >&2
    exit 1
  fi
}

cmd_start() {
  local short_id="$1"
  require_image; require_env; require_config "$short_id"

  local name; name="$(container_name "$short_id")"

  # If a container by this name already exists (running or stopped), remove it
  # so this command is idempotent.
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker rm -f "$name" >/dev/null
  fi

  docker run -d \
    --name "$name" \
    --label "${LABEL_KEY}=${LABEL_VALUE}" \
    --label "com.telegram-forwarder.user=${short_id}" \
    --restart unless-stopped \
    --env-file "$PROJECT_DIR/.env" \
    -v "$PROJECT_DIR/data/sessions:/data/sessions" \
    -v "$PROJECT_DIR/configs:/app/configs:ro" \
    "$IMAGE" \
    forward -c "/app/configs/${short_id}.json" >/dev/null

  echo "Started $name (configs/${short_id}.json)"
}

cmd_start_all() {
  shopt -s nullglob
  local any=0
  for cfg in "$PROJECT_DIR"/configs/*.json; do
    local base; base="$(basename "$cfg" .json)"
    # Skip example files
    if [[ "$base" == *.example ]]; then continue; fi
    cmd_start "$base"
    any=1
  done
  if [[ $any -eq 0 ]]; then
    echo "No configs/<short_id>.json files found (only examples)."
    exit 1
  fi
}

cmd_stop() {
  local short_id="$1"
  local name; name="$(container_name "$short_id")"
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    docker rm -f "$name" >/dev/null
    echo "Stopped $name"
  else
    echo "No container named $name"
  fi
}

cmd_stop_all() {
  local ids
  ids="$(docker ps -aq --filter "label=${LABEL_KEY}=${LABEL_VALUE}")"
  if [[ -z "$ids" ]]; then
    echo "No forwarder containers running."
    return 0
  fi
  echo "$ids" | xargs docker rm -f >/dev/null
  echo "Stopped all forwarder containers."
}

cmd_restart() {
  local short_id="$1"
  cmd_stop "$short_id" || true
  cmd_start "$short_id"
}

cmd_logs() {
  local short_id="$1"
  docker logs -f "$(container_name "$short_id")"
}

cmd_ps() {
  docker ps -a \
    --filter "label=${LABEL_KEY}=${LABEL_VALUE}" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Command}}'
}

cmd_status() {
  local short_id="$1"
  docker ps -a --filter "name=^/$(container_name "$short_id")$" \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Command}}'
}

usage() {
  sed -n '3,30p' "$0"
  exit 1
}

main() {
  [[ $# -eq 0 ]] && usage
  local cmd="$1"; shift || true

  case "$cmd" in
    start)      [[ $# -eq 1 ]] || usage; cmd_start "$1" ;;
    start-all)  [[ $# -eq 0 ]] || usage; cmd_start_all ;;
    stop)       [[ $# -eq 1 ]] || usage; cmd_stop "$1" ;;
    stop-all)   [[ $# -eq 0 ]] || usage; cmd_stop_all ;;
    restart)    [[ $# -eq 1 ]] || usage; cmd_restart "$1" ;;
    logs)       [[ $# -eq 1 ]] || usage; cmd_logs "$1" ;;
    ps)         [[ $# -eq 0 ]] || usage; cmd_ps ;;
    status)     [[ $# -eq 1 ]] || usage; cmd_status "$1" ;;
    -h|--help|help) usage ;;
    *) echo "Unknown command: $cmd" >&2; usage ;;
  esac
}

main "$@"
