#!/bin/bash
# Restart one Harness (or all) without killing a task that is running:
#   scripts/restart-harness.sh <Label|all> [--force]
# <Label> is the part after com.memtraceharness. in ~/Library/LaunchAgents (Beri, MemTrace, ...).
# `launchctl kickstart -k` kills the Harness at once: a running task's thread is lost and its provider CLI keeps
# running on its own. This script looks at workspace_locks first and skips a Harness that has a task running,
# unless --force. (Startup recovery then cleans up what a forced restart leaves behind and tells the operator.)
set -uo pipefail
target="${1:?usage: restart-harness.sh <Label|all> [--force]}"
force="${2:-}"
agents="$HOME/Library/LaunchAgents"
labels=()
if [ "$target" = "all" ]; then
  for plist in "$agents"/com.memtraceharness.*.plist; do
    name="$(basename "$plist" .plist)"; name="${name#com.memtraceharness.}"
    case "$name" in *.telegram|web) continue ;; esac
    labels+=("$name")
  done
else
  labels+=("$target")
fi
for label in "${labels[@]}"; do
  plist="$agents/com.memtraceharness.$label.plist"
  [ -f "$plist" ] || { echo "$label: no such launch agent"; continue; }
  root="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$plist" | awk '/-C/{getline; gsub(/^ +/,""); print; exit}')"
  running="$(sqlite3 "$root/.harness/harness.sqlite3" "select group_concat(conversation_id, ' ') from workspace_locks" 2>/dev/null || true)"
  if [ -n "$running" ] && [ "$force" != "--force" ]; then
    echo "$label: NOT restarted, task(s) running: $running (use --force to restart anyway)"
    continue
  fi
  launchctl kickstart -k "gui/$(id -u)/com.memtraceharness.$label" && echo "$label: restarted${running:+ (forced; interrupted: $running)}"
done
