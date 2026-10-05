"""Read-only TaiwanTrade proxy, exposed to agent CLIs as a stdio MCP server.

Why this exists: an agent CLI's sandbox blocks network access (Codex `read-only` /
`workspace-write` has none; a container's 127.0.0.1 is not the host's). MCP servers are
spawned by the CLI *outside* its tool sandbox, so this process can reach the TaiwanTrade
API on 127.0.0.1:8000 while the agent itself never gets a network or the API key.

Boundaries enforced here (not left to the prompt):
- Only the GET allowlist in ``TOOLS`` is reachable. Order, cancel, amend, watchlist-write,
  auth/key management and backtest-submission endpoints are deliberately absent, so an
  agent can research but never place or change an order.
- The API key is read from ``HARNESS_TAIWANTRADE_API_KEY_FILE`` (or the env var), never
  accepted as a tool argument and never echoed in a result or error.
- Path arguments are validated against a strict pattern before being substituted, so a
  ticker cannot smuggle in ``../trade/orders``.

Run: ``python -m memtrace_harness.taiwantrade_mcp`` (speaks JSON-RPC over stdin/stdout).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
MAX_RESPONSE_CHARS = 60_000
REQUEST_TIMEOUT_SECONDS = 30
PROTOCOL_VERSION = "2024-11-05"

_TICKER = r"[0-9A-Za-z]{1,10}"
_DATE = r"\d{4}-\d{2}-\d{2}"
_SYMBOLS = rf"{_TICKER}(,{_TICKER}){{0,49}}"


@dataclass(frozen=True)
class Param:
    name: str
    pattern: str
    description: str
    required: bool = True
    in_path: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    path: str  # may contain {placeholders} matching in_path params
    params: tuple[Param, ...] = ()


TOOLS: tuple[Tool, ...] = (
    Tool(
        "get_quotes",
        "Real-time quotes (read-only). symbols is comma separated, e.g. 6770,2409.",
        "/quotes",
        (Param("symbols", _SYMBOLS, "Comma-separated stock codes, max 50"),),
    ),
    Tool(
        "get_stock_daily",
        "Daily OHLCV for one stock between two dates (YYYY-MM-DD).",
        "/data/stocks/{ticker}/daily",
        (
            Param("ticker", _TICKER, "Stock code", in_path=True),
            Param("start_date", _DATE, "YYYY-MM-DD"),
            Param("end_date", _DATE, "YYYY-MM-DD"),
        ),
    ),
    Tool(
        "get_stock_intraday",
        "Intraday data for one stock on one date (last 30 days only).",
        "/data/stocks/{ticker}/intraday",
        (
            Param("ticker", _TICKER, "Stock code", in_path=True),
            Param("date", _DATE, "YYYY-MM-DD"),
        ),
    ),
    Tool(
        "get_index_daily",
        "Daily market index data. code is TAIEX.",
        "/data/index/{code}/daily",
        (
            Param("code", r"[A-Za-z]{1,10}", "Index code, e.g. TAIEX", in_path=True),
            Param("start_date", _DATE, "YYYY-MM-DD"),
            Param("end_date", _DATE, "YYYY-MM-DD"),
        ),
    ),
    Tool("get_market_snapshot", "Market close plus short-selling ratio snapshot.", "/data/market-snapshot"),
    Tool(
        "get_indicator",
        "A technical indicator series for one stock.",
        "/analysis/stocks/{ticker}/indicators/{indicator}",
        (
            Param("ticker", _TICKER, "Stock code", in_path=True),
            Param("indicator", r"[A-Za-z0-9_]{1,40}", "Indicator name", in_path=True),
        ),
    ),
    Tool(
        "get_daily_screen",
        "Daily general stock screen results (base_institutional / base_pullback).",
        "/analysis/daily-screen",
        (Param("strategy", r"[A-Za-z0-9_]{1,40}", "Screen strategy name", required=False),),
    ),
    Tool("get_institutional_breakout_screen", "Institutional-consensus breakout screen (after close).", "/analysis/institutional/breakout-screen"),
    Tool("get_overnight_screen", "Intraday overnight-trade screen (13:25 late-session momentum).", "/analysis/intraday/overnight-screen"),
    Tool("get_positions", "Current account positions (read-only).", "/trade/positions"),
    Tool("get_balance", "Account balance (read-only).", "/trade/balance"),
    Tool("get_settlements", "Settlement schedule (read-only).", "/trade/settlements"),
    Tool("list_orders", "List existing orders (read-only; cannot place or change any).", "/trade/orders"),
    Tool("get_watchlist", "Read the watchlist.", "/watchlist"),
    Tool(
        "get_backtest_run",
        "Fetch the summary of an already-finished backtest run.",
        "/backtests/{run_id}",
        (Param("run_id", r"[0-9A-Za-z_-]{1,64}", "Run id", in_path=True),),
    ),
)
_TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


class ProxyError(RuntimeError):
    pass


def load_api_key(env: dict[str, str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)
    key_file = env.get("HARNESS_TAIWANTRADE_API_KEY_FILE")
    if key_file:
        try:
            return Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProxyError(f"cannot read HARNESS_TAIWANTRADE_API_KEY_FILE: {exc.strerror}") from exc
    return env.get("HARNESS_TAIWANTRADE_API_KEY", "").strip()


def build_url(tool: Tool, arguments: dict[str, Any], base_url: str) -> str:
    unknown = set(arguments) - {param.name for param in tool.params}
    if unknown:
        raise ProxyError(f"unexpected arguments: {', '.join(sorted(unknown))}")
    path_values: dict[str, str] = {}
    query: list[tuple[str, str]] = []
    for param in tool.params:
        value = arguments.get(param.name)
        if value is None or value == "":
            if param.required:
                raise ProxyError(f"missing required argument: {param.name}")
            continue
        text = str(value)
        if not re.fullmatch(param.pattern, text):
            raise ProxyError(f"invalid value for {param.name}")
        if param.in_path:
            path_values[param.name] = urllib.parse.quote(text, safe="")
        else:
            query.append((param.name, text))
    url = base_url.rstrip("/") + tool.path.format(**path_values)
    return url + ("?" + urllib.parse.urlencode(query) if query else "")


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ProxyError(f"unknown tool: {name}")
    if not api_key:
        raise ProxyError("TaiwanTrade API key is not configured for the proxy")
    request = urllib.request.Request(
        build_url(tool, arguments, base_url),
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProxyError(f"TaiwanTrade returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProxyError(f"cannot reach TaiwanTrade: {getattr(exc, 'reason', exc)}") from exc
    if len(body) > MAX_RESPONSE_CHARS:
        body = body[:MAX_RESPONSE_CHARS] + f"\n...[truncated, {len(body)} chars total]"
    return body


def _tool_schema(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": {
            "type": "object",
            "properties": {
                param.name: {"type": "string", "description": param.description}
                for param in tool.params
            },
            "required": [param.name for param in tool.params if param.required],
            "additionalProperties": False,
        },
    }


def handle_message(
    message: dict[str, Any],
    *,
    base_url: str,
    api_key_loader: Callable[[], str],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:  # notification (e.g. notifications/initialized) — no reply
        return None

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "taiwantrade-readonly", "version": "1"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [_tool_schema(tool) for tool in TOOLS]})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            text = call_tool(
                str(params.get("name", "")),
                params.get("arguments") or {},
                base_url=base_url,
                api_key=api_key_loader(),
                opener=opener,
            )
            return ok({"content": [{"type": "text", "text": text}]})
        except ProxyError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    base_url = os.getenv("HARNESS_TAIWANTRADE_API_URL", DEFAULT_BASE_URL)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle_message(message, base_url=base_url, api_key_loader=load_api_key)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def mcp_server_spec(
    env: dict[str, str] | None = None, *, python: str | None = None
) -> dict[str, Any] | None:
    """Launch spec for this server, or None when the operator hasn't opted in.

    Opt-in is HARNESS_TAIWANTRADE_API_KEY_FILE — only a *path* is handed to the CLI, so the
    key never appears in a command line or MCP config.
    """
    env = env if env is not None else dict(os.environ)
    key_file = env.get("HARNESS_TAIWANTRADE_API_KEY_FILE")
    if not key_file:
        return None
    server_env = {"HARNESS_TAIWANTRADE_API_KEY_FILE": key_file}
    if env.get("HARNESS_TAIWANTRADE_API_URL"):
        server_env["HARNESS_TAIWANTRADE_API_URL"] = env["HARNESS_TAIWANTRADE_API_URL"]
    return {
        "command": python or sys.executable,
        "args": ["-m", "memtrace_harness.taiwantrade_mcp"],
        "env": server_env,
    }


if __name__ == "__main__":
    serve()
