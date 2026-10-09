#!/bin/bash
# Run one Harness in the foreground: scripts/run-harness.sh <workspace-dir>
# (launchd runs the same thing; see `memtrace-harness migrate` / docs/services.md)
set -euo pipefail
workspace="${1:?usage: run-harness.sh <workspace-dir>}"
cd "$(dirname "$0")/.."
exec ./.venv/bin/python -m memtrace_harness -C "$workspace" serve
