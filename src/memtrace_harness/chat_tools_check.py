"""Startup check that every project's chat models can actually reach MemTrace.

The chat model is told it can look things up in MemTrace. Until 2026-10-09 a Codex chat model
(own CODEX_HOME, no MCP servers) had no such tool and nothing noticed: it told the operator
"I have no MemTrace tools" while the Harness believed it did. This only *reports*.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig

CHECK_TIMEOUT_SECONDS = 30


def _run_claude_mcp_list(executable: str) -> str:
    result = subprocess.run(
        [executable, "mcp", "list"], capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200] or f"exit {result.returncode}")
    return result.stdout


def claude_memtrace_status(listing: str) -> str | None:
    """The `claude mcp list` line of the server named memtrace, or None when absent."""
    for line in listing.splitlines():
        if line.strip().split(":", 1)[0].strip().lower() == "memtrace":
            return line.strip()
    return None


def check_chat_tools(
    config: HarnessConfig,
    project_names: Iterable[str],
    *,
    run: Callable[[str], str] = _run_claude_mcp_list,
) -> list[str]:
    """Problems found, as human-readable lines; empty when every chat candidate can read MemTrace.
    Antigravity candidates are covered by agy_mcp_check."""
    import os

    from memtrace_harness.adapters.codex import MEMTRACE_TOKEN_ENV

    problems: list[str] = []
    claude_listing: str | None = None
    claude_error: str | None = None
    for project in project_names:
        for provider, model in config.chat_candidates_for(project):
            label = f"[{project}] chat model {provider}/{model or 'default'}"
            if provider == "codex":
                command = config.chat_command("codex", model, "x", memtrace_read=True)
                if not any(arg.startswith("mcp_servers.memtrace.url=") for arg in command):
                    problems.append(f"{label} has no MemTrace tools (MEMTRACE_MCP_URL is not set).")
                elif not os.getenv(MEMTRACE_TOKEN_ENV):
                    problems.append(f"{label} would call MemTrace without a token ({MEMTRACE_TOKEN_ENV} is not set).")
            elif provider == "claude":
                if claude_listing is None and claude_error is None:
                    try:
                        claude_listing = run(config.command_for("claude"))
                    except Exception as exc:  # the check must never stop the gateway
                        claude_error = str(exc)
                if claude_error is not None:
                    problems.append(f"{label}: could not list Claude MCP servers ({claude_error}).")
                    continue
                status = claude_memtrace_status(claude_listing or "")
                if status is None:
                    problems.append(f"{label} has no 'memtrace' MCP server registered in Claude (claude mcp add).")
                elif "✔" not in status and "Connected" not in status:
                    problems.append(f"{label}: Claude's memtrace MCP server is not connected ({status[-60:]}).")
    return list(dict.fromkeys(problems))
