#!/bin/bash
# Run one independent Harness instance: scripts/run-instance.sh <name>
# Everything per-instance (env, project index, sqlite, traces, status port, bot token,
# workspaces) lives in instances/<name>/ — nothing is shared with other instances except
# the code and .venv.
set -euo pipefail
name="${1:?usage: run-instance.sh <instance-name>}"
cd "$(dirname "$0")/.."
set -a
source "instances/$name/.env"
set +a
mkdir -p "instances/$name/data"
exec ./.venv/bin/python -m memtrace_harness gateway --serve \
    --scan-interval-seconds "${SCAN_INTERVAL_SECONDS:-1800}"
