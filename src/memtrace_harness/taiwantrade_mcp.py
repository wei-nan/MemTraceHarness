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

Orders (opt-in per project, ``HARNESS_TAIWANTRADE_ORDER_PROJECTS``): the only write-ish tool
is ``create_order_intent`` (and ``request_order_cancel`` for an existing order). They create a
TaiwanTrade *intent* / a cancel *request* — nothing reaches the broker — and hand the one-time
confirmation token to the harness database, never to the model. Only the gateway can turn either
into a real order or cancellation, and only when the human taps the Telegram confirm button
(``submit_order`` / ``cancel_order``). So a trade always needs the human, however the model behaves.

Run: ``python -m memtrace_harness.taiwantrade_mcp`` (speaks JSON-RPC over stdin/stdout).
"""

from __future__ import annotations

import hashlib
import json
import uuid
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------- orders

# Harness-side ceiling per order, on top of TaiwanTrade's own risk limits. Set
# HARNESS_TAIWANTRADE_MAX_ORDER_VALUE (TWD) to change it.
DEFAULT_MAX_ORDER_VALUE = 100_000
INTENT_IDEMPOTENCY_WINDOW_SECONDS = 300
LOT_SIZE = 1000

ORDER_TOOL_NAME = "create_order_intent"
ORDER_TOOL = Tool(
    ORDER_TOOL_NAME,
    "Propose a stock order for the user to confirm. This does NOT place the order: it creates a "
    "pending order and the user gets a confirm/cancel button in Telegram; only their tap sends it "
    "to the broker, and the result is reported to them automatically. Limit orders only. For an "
    "intraday odd-lot order set is_odd_lot=true and quantity is in shares (1-999); otherwise "
    "quantity is in lots (張, 1000 shares each).",
    "/trade/order-intents",
    (
        Param("symbol", r"\d{4,6}", "Taiwan stock code, e.g. 2327"),
        Param("action", r"Buy|Sell", "Buy or Sell"),
        Param("price", r"\d{1,6}(\.\d{1,2})?", "Limit price per share, > 0"),
        Param("quantity", r"\d{1,4}", "Shares if is_odd_lot=true, otherwise lots"),
        Param("is_odd_lot", r"true|false", "true for intraday odd-lot (shares)", required=False),
        Param("order_type", r"ROD|IOC|FOK", "ROD (default) / IOC / FOK; odd-lot is ROD only", required=False),
    ),
)


CANCEL_TOOL_NAME = "request_order_cancel"
CANCEL_TOOL = Tool(
    CANCEL_TOOL_NAME,
    "Propose cancelling an existing order for the user to confirm. This does NOT cancel it: the user "
    "gets a confirm/cancel button in Telegram and only their tap sends the cancellation to the broker; "
    "the result is reported to them automatically. Find the order_id with list_orders first. The local "
    "order status can lag the broker (a filled order may still read PendingSubmit); get_positions shows "
    "what is actually held.",
    "/trade/orders",
    (Param("order_id", r"[0-9A-Za-z_-]{1,64}", "Order id from list_orders"),),
)
_TERMINAL_STATUSES = ("Filled", "Cancelled", "Failed", "Rejected", "Inactive")
CANCEL_REQUEST_TTL_SECONDS = 300


@dataclass(frozen=True)
class OrderContext:
    """What the proxy needs to propose orders. Present only for a project the operator
    explicitly allowed (see mcp_server_spec); absent means the order tool does not exist."""

    project: str
    store: Any  # TraceStore
    max_order_value: float = DEFAULT_MAX_ORDER_VALUE


def _order_args(arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = set(arguments) - {p.name for p in ORDER_TOOL.params}
    if unknown:
        raise ProxyError(f"unexpected arguments: {', '.join(sorted(unknown))}")
    values: dict[str, Any] = {}
    for param in ORDER_TOOL.params:
        raw = arguments.get(param.name)
        if raw is None or raw == "":
            if param.required:
                raise ProxyError(f"missing required argument: {param.name}")
            continue
        text = str(raw).strip().lower() if param.name == "is_odd_lot" else str(raw).strip()
        if not re.fullmatch(param.pattern, text):
            raise ProxyError(f"invalid value for {param.name}")
        values[param.name] = text
    return values


def create_order_intent(
    arguments: dict[str, Any],
    *,
    context: OrderContext,
    base_url: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: float | None = None,
) -> str:
    """Create the intent at TaiwanTrade and park its confirmation token in the harness DB.
    Returns the model-facing text: the proposal's details, never the token."""
    if not api_key:
        raise ProxyError("TaiwanTrade API key is not configured for the proxy")
    args = _order_args(arguments)
    odd_lot = args.get("is_odd_lot", "false") == "true"
    price = float(args["price"])
    quantity = int(args["quantity"])
    if price <= 0 or quantity <= 0:
        raise ProxyError("price and quantity must be positive")
    estimated = round(price * quantity * (1 if odd_lot else LOT_SIZE), 2)
    if estimated > context.max_order_value:
        raise ProxyError(
            f"order value {estimated:,.0f} TWD is above the harness limit of "
            f"{context.max_order_value:,.0f} TWD per order; ask the user to place it themselves "
            "or to raise HARNESS_TAIWANTRADE_MAX_ORDER_VALUE"
        )
    order_type = args.get("order_type", "ROD")
    body = {
        "symbol": args["symbol"],
        "action": args["action"],
        "price": price,
        "quantity": quantity,
        "price_type": "LMT",
        "order_type": order_type,
        "is_odd_lot": odd_lot,
    }
    # The same proposal repeated within the intent's lifetime (a model retry) must map to the
    # same intent, or the human would be asked to confirm one order twice.
    bucket = int((time.time() if now is None else now) // INTENT_IDEMPOTENCY_WINDOW_SECONDS)
    fingerprint = json.dumps([context.project, body, bucket], sort_keys=True)
    idempotency_key = "harness-" + hashlib.sha256(fingerprint.encode()).hexdigest()[:40]

    request = urllib.request.Request(
        base_url.rstrip("/") + "/trade/order-intents",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "X-API-KEY": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    intent = _send(request, opener)
    intent_id = str(intent.get("intent_id") or "")
    token = intent.get("confirmation_token")
    if not intent_id:
        raise ProxyError("TaiwanTrade did not return an intent id")
    status = str(intent.get("status") or "")
    if token:
        context.store.create_order_intent(
            intent_id=intent_id,
            project=context.project,
            symbol=body["symbol"],
            action=body["action"],
            price=price,
            quantity=quantity,
            is_odd_lot=odd_lot,
            price_type="LMT",
            order_type=order_type,
            estimated_value=estimated,
            risk=intent.get("risk_snapshot") or {},
            confirmation_token=str(token),
            expires_at=str(intent.get("expires_at") or ""),
        )
        message = (
            "Order proposal created and NOT yet placed. The user has been sent a confirm/cancel "
            "button in Telegram; the order is only sent if they tap confirm, and the result is "
            "reported to them automatically. Tell the user to check Telegram."
        )
    else:
        # Same proposal as one already issued (a retry, or the user asking again). If it is
        # still waiting for the human, show the confirm buttons again where they will see them.
        reshown = context.store.reshow_order_intent(intent_id)
        message = (
            "This exact proposal already exists and is still waiting for the user; its confirm/cancel "
            "buttons were sent to their Telegram again. Tell the user to check the latest Telegram "
            "message. Do not create it again."
            if reshown
            else f"An identical proposal was already created in the last few minutes (status {status}); "
            "no new confirmation was issued. Do not create it again."
        )
    return json.dumps(
        {
            "intent_id": intent_id,
            "status": status,
            "symbol": body["symbol"],
            "action": body["action"],
            "price": price,
            "quantity": quantity,
            "unit": "shares (odd lot)" if odd_lot else "lots",
            "estimated_value_twd": estimated,
            "expires_at": intent.get("expires_at"),
            "note": message,
        },
        ensure_ascii=False,
    )


def create_cancel_request(
    arguments: dict[str, Any],
    *,
    context: OrderContext,
    base_url: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: datetime | None = None,
) -> str:
    """Record a proposal to cancel an existing order. Reads today's orders (a GET) to find it
    and show the human what would be cancelled; sends nothing to the broker."""
    if not api_key:
        raise ProxyError("TaiwanTrade API key is not configured for the proxy")
    unknown = set(arguments) - {"order_id"}
    if unknown:
        raise ProxyError(f"unexpected arguments: {', '.join(sorted(unknown))}")
    order_id = str(arguments.get("order_id") or "").strip()
    if not re.fullmatch(CANCEL_TOOL.params[0].pattern, order_id):
        raise ProxyError("invalid value for order_id")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/trade/orders",
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
        method="GET",
    )
    with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        orders = json.loads(response.read().decode("utf-8", errors="replace"))
    match = next((o for o in orders if str(o.get("order_id")) == order_id), None)
    if match is None:
        raise ProxyError(f"no order {order_id} among today's orders; check list_orders")
    status = str(match.get("status") or "")
    if status.rsplit(".", 1)[-1] in _TERMINAL_STATUSES:
        raise ProxyError(f"order {order_id} is already {status}; there is nothing to cancel")
    existing = context.store.find_open_cancel_request(order_id)
    if existing:
        context.store.reshow_order_intent(existing["intent_id"])
        request_id = existing["intent_id"]
        note = (
            "A cancel proposal for this order is already waiting for the user; its buttons were "
            "sent to their Telegram again. Tell the user to check the latest Telegram message. "
            "Do not create it again."
        )
    else:
        request_id = "cancel-" + uuid.uuid4().hex[:16]
        expires = (now or datetime.now(timezone.utc)) + timedelta(seconds=CANCEL_REQUEST_TTL_SECONDS)
        context.store.create_cancel_request(
            intent_id=request_id,
            project=context.project,
            target_order_id=order_id,
            symbol=str(match.get("symbol")),
            action=str(match.get("action")),
            price=float(match.get("price") or 0),
            quantity=int(match.get("quantity") or 0),
            is_odd_lot=bool(match.get("is_odd_lot")),
            expires_at=expires.isoformat(),
        )
        note = (
            "Cancel proposal created and NOT yet sent. The user has been sent a confirm/cancel "
            "button in Telegram; the cancellation is only sent if they tap confirm, and the result "
            "is reported to them automatically. Tell the user to check Telegram."
        )
    return json.dumps(
        {
            "request_id": request_id,
            "order_id": order_id,
            "symbol": match.get("symbol"),
            "action": match.get("action"),
            "price": match.get("price"),
            "quantity": match.get("quantity"),
            "order_status_as_recorded": status,
            "note": note,
        },
        ensure_ascii=False,
    )


def cancel_order(
    order_id: str,
    *,
    base_url: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Send a confirmed cancellation. Called by the gateway after the human's tap — never
    reachable from a tool."""
    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", order_id):
        raise ProxyError("invalid order id")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/trade/orders/" + urllib.parse.quote(order_id, safe=""),
        headers={"X-API-KEY": api_key, "Accept": "application/json"},
        method="DELETE",
    )
    return _send(request, opener)


def submit_order(
    intent_id: str,
    token: str,
    *,
    base_url: str,
    api_key: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Turn a confirmed intent into a real order. Called by the gateway after the human's tap
    — never reachable from a tool, so a model cannot call it."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/trade/orders",
        data=json.dumps({"intent_id": intent_id, "confirmation_token": token}).encode("utf-8"),
        headers={
            "X-API-KEY": api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    return _send(request, opener)


def _send(request: urllib.request.Request, opener: Callable[..., Any]) -> dict[str, Any]:
    try:
        with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ProxyError(f"TaiwanTrade returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProxyError(f"cannot reach TaiwanTrade: {getattr(exc, 'reason', exc)}") from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProxyError("TaiwanTrade returned a non-JSON response") from exc
    if not isinstance(data, dict):
        raise ProxyError("TaiwanTrade returned an unexpected response")
    return data


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
    order_context: OrderContext | None = None,
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
        tools = [*TOOLS, ORDER_TOOL, CANCEL_TOOL] if order_context else TOOLS
        return ok({"tools": [_tool_schema(tool) for tool in tools]})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            if order_context and params.get("name") == ORDER_TOOL_NAME:
                text = create_order_intent(
                    params.get("arguments") or {},
                    context=order_context,
                    base_url=base_url,
                    api_key=api_key_loader(),
                    opener=opener,
                )
                return ok({"content": [{"type": "text", "text": text}]})
            if order_context and params.get("name") == CANCEL_TOOL_NAME:
                text = create_cancel_request(
                    params.get("arguments") or {},
                    context=order_context,
                    base_url=base_url,
                    api_key=api_key_loader(),
                    opener=opener,
                )
                return ok({"content": [{"type": "text", "text": text}]})
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
    order_context = _order_context_from_env(os.environ)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle_message(
            message, base_url=base_url, api_key_loader=load_api_key, order_context=order_context
        )
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def tool_catalog(env: dict[str, str] | None = None) -> str:
    """The TaiwanTrade tools every Agent Loop role has, and what they do NOT cover, built from
    the tool definitions themselves so it cannot drift from what is actually served. Empty when
    the operator has not opted in (no API key file), because then no role has the tools."""
    if not mcp_server_spec(env):
        return ""
    lines = [f"- {tool.name}: {tool.description}" for tool in TOOLS]
    return (
        "TaiwanTrade tools available to you (read-only MCP, they add the API key themselves):\n"
        + "\n".join(lines)
        + f"\nNOT available to Agent Loop roles: `{ORDER_TOOL_NAME}` and `{CANCEL_TOOL_NAME}` "
        "(only the Telegram chat model proposes orders, and a human taps to confirm), and any "
        "TaiwanTrade endpoint not listed above (for example chart images).\n"
    )


def order_projects(env: dict[str, str] | None = None) -> set[str]:
    """Projects the operator allowed to propose orders (HARNESS_TAIWANTRADE_ORDER_PROJECTS,
    comma-separated project names). Empty by default: nobody can trade."""
    env = env if env is not None else dict(os.environ)
    raw = env.get("HARNESS_TAIWANTRADE_ORDER_PROJECTS", "")
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def _order_context_from_env(env: Any) -> OrderContext | None:
    project = env.get("HARNESS_ORDER_PROJECT")
    db_path = env.get("HARNESS_TRACE_DB")
    if not project or not db_path:
        return None
    from memtrace_harness.trace_store import TraceStore

    try:
        limit = float(env.get("HARNESS_TAIWANTRADE_MAX_ORDER_VALUE") or DEFAULT_MAX_ORDER_VALUE)
    except ValueError:
        limit = DEFAULT_MAX_ORDER_VALUE
    return OrderContext(project=project, store=TraceStore(Path(db_path)), max_order_value=limit)


def mcp_server_spec(
    env: dict[str, str] | None = None,
    *,
    python: str | None = None,
    order_project: str | None = None,
    trace_db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Launch spec for this server, or None when the operator hasn't opted in.

    Opt-in is HARNESS_TAIWANTRADE_API_KEY_FILE — only a *path* is handed to the CLI, so the
    key never appears in a command line or MCP config. ``order_project`` additionally turns on
    the order-proposal tool, but only if that project is listed in
    HARNESS_TAIWANTRADE_ORDER_PROJECTS and the caller says where the harness database is.
    """
    env = env if env is not None else dict(os.environ)
    key_file = env.get("HARNESS_TAIWANTRADE_API_KEY_FILE")
    if not key_file:
        return None
    server_env = {"HARNESS_TAIWANTRADE_API_KEY_FILE": key_file}
    if env.get("HARNESS_TAIWANTRADE_API_URL"):
        server_env["HARNESS_TAIWANTRADE_API_URL"] = env["HARNESS_TAIWANTRADE_API_URL"]
    if order_project and trace_db_path and order_project.lower() in order_projects(env):
        server_env["HARNESS_ORDER_PROJECT"] = order_project
        server_env["HARNESS_TRACE_DB"] = str(Path(trace_db_path).resolve())
        if env.get("HARNESS_TAIWANTRADE_MAX_ORDER_VALUE"):
            server_env["HARNESS_TAIWANTRADE_MAX_ORDER_VALUE"] = env["HARNESS_TAIWANTRADE_MAX_ORDER_VALUE"]
    return {
        "command": python or sys.executable,
        "args": ["-m", "memtrace_harness.taiwantrade_mcp"],
        "env": server_env,
    }


if __name__ == "__main__":
    serve()
