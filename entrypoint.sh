#!/usr/bin/env bash
set -e

# Make sure the session directory exists and is writable
mkdir -p "${SESSION_DIR:-/data/sessions}"

exec python -m src.main "$@"
