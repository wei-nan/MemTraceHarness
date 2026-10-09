#!/bin/bash
# Run one Telegram service in the foreground: scripts/run-telegram.sh <telegram.env>
set -euo pipefail
envfile="${1:?usage: run-telegram.sh <telegram.env>}"
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m memtrace_services.telegram.main --env-file "$envfile"
