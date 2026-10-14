#!/bin/bash
# Run one A2A service in the foreground: scripts/run-a2a.sh <a2a.toml>
# (one per Harness; it is what lets other Harnesses ask this one, and this one ask them: docs/a2a-peers.md)
set -euo pipefail
config="${1:?usage: run-a2a.sh <a2a.toml>}"
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m memtrace_services.a2a.main --config "$config"
