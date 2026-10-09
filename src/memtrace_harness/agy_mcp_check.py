"""Startup check that Antigravity has the MCP servers the Harness expects it to have.

Claude and Codex get the TaiwanTrade tools injected on every call (adapters/claude.py,
codex.py). Antigravity does not: its servers are registered once, by hand, with `agy mcp add`,
so a fresh machine, a reinstall or a typo silently leaves its roles without the tools — and the
tool-first prompt then points them at tools they do not have. This only *reports*; it never
registers anything itself.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

from memtrace_harness.taiwantrade_mcp import mcp_server_spec

CHECK_TIMEOUT_SECONDS = 15
AGY_MCP_CONFIG = Path.home() / ".gemini" / "config" / "mcp_config.json"
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "harness-check", "version": "0"}},
}


def _same_memtrace_endpoint(registered: str, expected: str) -> bool:
    """The server serves the same MCP at both `<origin>/mcp` and `<origin>/api/v1/mcp/mcp`."""

    def normalise(url: str) -> str:
        url = url.strip().rstrip("/")
        return url.removesuffix("/api/v1/mcp/mcp").removesuffix("/mcp")

    return normalise(registered) == normalise(expected)


def _probe_memtrace_key(config_path: Path = AGY_MCP_CONFIG) -> str | None:
    """Send an MCP initialize with the credentials agy has registered. Returns a problem
    line when the server rejects them, None when accepted or when the probe itself cannot
    run (no config / network) — an unreachable server is not evidence of a bad key."""
    try:
        entry = json.loads(config_path.read_text())["mcpServers"]["memtrace"]
        url = entry.get("serverUrl") or entry["url"]
        headers = {**entry.get("headers", {}), "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        request = urllib.request.Request(url, json.dumps(_INITIALIZE).encode(), headers)
        urllib.request.urlopen(request, timeout=CHECK_TIMEOUT_SECONDS).close()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return (
                f"Antigravity's MemTrace MCP credentials were rejected (HTTP {exc.code}); its roles cannot reach "
                "MemTrace. Re-register with `agy mcp add --header \"Authorization: Bearer <key>\" "
                "--header \"X-MemTrace-Tool-Profile: core+agent_loop\" memtrace <MEMTRACE_MCP_URL>`."
            )
    except Exception:
        pass
    return None


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
    probe: Callable[[], str | None] = _probe_memtrace_key,
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
        elif (not _same_memtrace_endpoint(entry["command"], needle) if name == "memtrace" else needle not in entry["command"]):
            problems.append(
                f"Antigravity MCP server '{name}' points at '{entry['command']}', not the expected '{needle}'."
            )
    if "memtrace" in expected and "memtrace" in registered:
        key_problem = probe()
        if key_problem:
            problems.append(key_problem)
    return problems
