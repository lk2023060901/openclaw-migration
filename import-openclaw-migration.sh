#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec python3 "$SCRIPT_DIR/openclaw_migration_bundle.py" import "$@"
