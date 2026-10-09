#!/bin/bash
# Run the status page in the foreground: scripts/run-web.sh <web.toml>
set -euo pipefail
config="${1:?usage: run-web.sh <web.toml>}"
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m memtrace_services.web.main --config "$config"
