"""Read-only GitHub Actions lookups for the chat model (tier ``read``, operator request 2026-10-12).

Why this exists: the chat model's sandbox has no network and ``browse`` has no logins, so a private repo's run page
is "Page not found" there. The Harness runs ``gh`` itself (already logged in on the host) for a fixed set of
read-only views. **Enforced:** the repo must be one of the project's own (``github_issue_repos``); the argv is a list
with fixed sub-commands, never a shell string; ids are digits only; the output is token-masked and clipped.
Nothing here writes to GitHub.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from memtrace_harness import tool_registry
from memtrace_harness.github_actions import _repo
from memtrace_harness.ops_read import redact

COMMAND_SECONDS = 60
MAX_OUTPUT_CHARS = 12000
_RUN_ID = re.compile(r"^[0-9]{1,20}$")
_BRANCH = re.compile(r"^[A-Za-z0-9_./-]{1,100}$")
_ACTIONS = ("list", "view", "failed_log")


def _gh(argv: list[str]) -> str:
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "LC_ALL": "C", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]  # gh keeps its login under ~/.config/gh and the keychain
    try:
        done = subprocess.run(
            ["gh", *argv], capture_output=True, text=True, timeout=COMMAND_SECONDS, stdin=subprocess.DEVNULL, env=env
        )
    except subprocess.TimeoutExpired as exc:
        raise tool_registry.OpsError(f"gh timed out after {COMMAND_SECONDS}s") from exc
    except OSError as exc:
        raise tool_registry.OpsError(f"gh could not run: {exc}") from exc
    text = ((done.stdout or "") + (done.stderr or "")).strip()
    if done.returncode != 0:
        raise tool_registry.OpsError(redact(text or f"gh exited {done.returncode}")[:2000])
    return text


def _tail(text: str) -> str:
    # A failing test's reason is at the end of its log, so keep the end, not the start.
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return f"…（共 {len(text)} 字，只保留最後 {MAX_OUTPUT_CHARS} 字）\n" + text[-MAX_OUTPUT_CHARS:]


def github_ci(ctx: Any, arguments: dict[str, Any]) -> str:
    action = str(arguments.get("action") or "")
    if action not in _ACTIONS:
        raise tool_registry.OpsError(f"action must be one of: {', '.join(_ACTIONS)}")
    try:
        repo = _repo(list(ctx.github_repos), arguments.get("repo") or (ctx.github_repos[0] if len(ctx.github_repos) == 1 else ""))
    except ValueError as exc:
        raise tool_registry.OpsError(str(exc)) from exc
    if action == "list":
        argv = ["run", "list", "-R", repo, "-L", "15"]
        branch = str(arguments.get("branch") or "").strip()
        if branch:
            if not _BRANCH.match(branch):
                raise tool_registry.OpsError("branch has characters that are not allowed")
            argv += ["--branch", branch]
        return redact(_tail(_gh(argv)))
    run_id = str(arguments.get("run_id") or "").strip().lstrip("#")
    if not _RUN_ID.match(run_id):
        raise tool_registry.OpsError("run_id must be the numeric Actions run id (the number after /actions/runs/)")
    argv = ["run", "view", run_id, "-R", repo]
    if action == "failed_log":
        argv.append("--log-failed")
    return redact(_tail(_gh(argv)))


tool_registry.register(
    tool_registry.ToolSpec(
        name="github_ci",
        schema={
            "name": "github_ci",
            "description": (
                "Read GitHub Actions for this project's own repo, using the host's logged-in gh (works for private repos, "
                "unlike browse). Read-only. action=list: recent runs (optional branch). action=view: one run's status, "
                "conclusion and per-job steps. action=failed_log: the log of the failing steps only (end of the log kept). "
                "run_id is the number in .../actions/runs/<run_id>; a job id from a /job/ URL is not a run id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_ACTIONS)},
                    "repo": {"type": "string", "description": "owner/repo; optional when the project has one repo"},
                    "run_id": {"type": "string", "description": "numeric run id (view, failed_log)"},
                    "branch": {"type": "string", "description": "list only: filter by branch"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        handler=github_ci,
        tier=tool_registry.READ,
        available=lambda c: bool(c.github_repos),
    )
)
