"""Startup check that Antigravity has the MCP servers the Harness expects it to have.

Claude and Codex get the TaiwanTrade tools injected on every call (adapters/claude.py,
codex.py). Antigravity does not: its servers are registered once, by hand, with `agy mcp add`,
so a fresh machine, a reinstall or a typo silently leaves its roles without the tools — and the
tool-first prompt then points them at tools they do not have. This only *reports*; it never
registers anything itself.
"""

from __future__ import annotations

from collections.abc import Callable
import shlex
import shutil
import subprocess
from typing import Any

from memtrace_harness.taiwantrade_mcp import mcp_server_spec

CHECK_TIMEOUT_SECONDS = 15


def _run_agy_list(executable: str) -> str:
    result = subprocess.run(
        [executable, "mcp", "list"], capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS, check=False
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200] or f"exit {result.returncode}")
    return result.stdout


def parse_mcp_list(output: str) -> dict[str, dict[str, str]]:
    """`agy mcp list` prints NAME TYPE STATUS COMMAND/URL; the command may contain spaces."""
    servers: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or parts[0] == "NAME":
            continue
        servers[parts[0]] = {
            "type": parts[1],
            "status": parts[2].lower(),
            "command": parts[3].strip() if len(parts) > 3 else "",
        }
    return servers


def expected_servers(memtrace_mcp_url: str | None = None) -> dict[str, str]:
    """name -> text the registered command/URL must contain."""
    expected: dict[str, str] = {}
    spec = mcp_server_spec()
    if spec:
        expected["taiwantrade"] = " ".join([spec["command"], *spec["args"]])
    if memtrace_mcp_url:
        expected["memtrace"] = memtrace_mcp_url
    return expected


def register_hint(name: str, spec: dict[str, Any]) -> str:
    env = "".join(f"-e {shlex.quote(f'{k}={v}')} " for k, v in spec["env"].items() if k != "HARNESS_TRACE_DB")
    return f"agy mcp add {env}{name} {shlex.quote(spec['command'])} {' '.join(spec['args'])}"


def check_antigravity_mcp(
    memtrace_mcp_url: str | None = None,
    *,
    executable: str = "agy",
    run: Callable[[str], str] = _run_agy_list,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Problems found, as human-readable lines; empty when fine or when agy is not installed
    (then no role can be using it)."""
    expected = expected_servers(memtrace_mcp_url)
    if not expected or not which(executable):
        return []
    try:
        registered = parse_mcp_list(run(executable))
    except Exception as exc:  # the check must never stop the gateway
        return [f"Antigravity: could not list MCP servers ({exc}); its roles may lack the Harness tools."]
    problems: list[str] = []
    spec = mcp_server_spec()
    for name, needle in expected.items():
        entry = registered.get(name)
        if entry is None:
            hint = f" Register it with: {register_hint(name, spec)}" if name == "taiwantrade" and spec else ""
            problems.append(f"Antigravity has no MCP server '{name}'.{hint}")
        elif entry["status"] != "enabled":
            problems.append(f"Antigravity MCP server '{name}' is {entry['status']}, not enabled (agy mcp enable {name}).")
        elif needle not in entry["command"]:
            problems.append(
                f"Antigravity MCP server '{name}' points at '{entry['command']}', not the expected '{needle}'."
            )
    return problems
