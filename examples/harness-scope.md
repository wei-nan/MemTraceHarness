# Harness scope — MemTraceHarness

<!-- Example. Copy to the root of your own checkout as harness-scope.md and edit the paths; the real file is not tracked. -->

- name: MemTraceHarness
- workspace_id: ws_ef463f70
- working_directory: /path/to/MemTraceHarness
- off_limits: .harness.env, .harness
- job: harness_restart = plist="$HOME/Library/LaunchAgents/com.memtraceharness.MemTraceHarness.plist"; label=$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist") || exit; launchctl kickstart -k "gui/$(id -u)/$label"
- job: other_harnesses_restart = uid=$(id -u); for plist in "$HOME"/Library/LaunchAgents/com.memtraceharness.*.plist; do [ -f "$plist" ] || continue; label=$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist" 2>/dev/null) || continue; [ "$label" = com.memtraceharness.MemTraceHarness ] && continue; case "$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments' "$plist" 2>/dev/null)" in *memtrace_harness*serve*) launchctl kickstart -k "gui/$uid/$label" >/dev/null 2>&1 && echo "restarted $label" || echo "FAILED $label" ;; esac; done

- job: harness_services_status = for l in $(launchctl list | awk '/com.memtraceharness/{print $3}'); do printf "%s  %s\n" "$(launchctl list | awk -v l=$l '$3==l{print "pid="$1" exit="$2}')" "$l"; done
- read_roots: ~/.config/memtraceharness, ~/Library/LaunchAgents, ~/Workspace/<other-project>
- github_repo: wei-nan/MemTraceHarness
- verify_command: PYTHONPATH=src python3 -m unittest discover -s tests
