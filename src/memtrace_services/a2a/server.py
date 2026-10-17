"""HTTP front of the A2A service: the Agent Card, and POST /rpc behind a per-peer bearer token."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memtrace_services.a2a.config import A2aConfig, PeerEntry
from memtrace_services.a2a.protocol import (
    INVALID_REQUEST, PARSE_ERROR, PROTOCOL_VERSION, VERSION_NOT_SUPPORTED, Dispatcher, agent_card,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 100_000
CARD_PATH = "/.well-known/agent-card.json"
RPC_PATH = "/rpc"
TASK_POLL_RATE_PER_MIN = 120  # two active questions polling every two seconds, with headroom


class RateLimiter:
    """At most `limit` calls per peer and budget per rolling minute."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, peer: str, limit: int, *, budget: str = "requests") -> bool:
        now = self._clock()
        with self._lock:
            calls = self._calls[peer, budget]
            while calls and now - calls[0] >= 60:
                calls.popleft()
            if len(calls) >= limit:
                return False
            calls.append(now)
            return True


class ProfileCache:
    """The Harness's own name and self-description, asked for at most every `ttl` seconds: the card is public and fetched often,
    and an edit on the status page should show within a minute. A Harness that is down leaves the last text (or none)."""

    def __init__(self, gateway, ttl: float = 30.0, clock=time.monotonic) -> None:
        self._gateway, self._ttl, self._clock = gateway, ttl, clock
        self._profile: dict = {}
        self._at: float | None = None
        self._lock = threading.Lock()

    def _current(self) -> dict:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._ttl:
                self._at = now
                try:
                    self._profile = dict(self._gateway.profile())
                except Exception:  # noqa: BLE001 — the card must still be served
                    logger.warning("could not read the Harness's profile; serving the last one")
            return self._profile

    def description(self) -> str | None:
        text = self._current().get("description")
        return str(text) if text is not None else None

    def name(self) -> str | None:
        text = self._current().get("name")
        return str(text) if text else None

    def identity(self) -> str | None:
        value = self._current().get("id")
        return str(value) if value else None


class _Handler(BaseHTTPRequestHandler):
    config: A2aConfig
    dispatcher: Dispatcher
    limiter: RateLimiter
    profile: ProfileCache | None = None

    def _send(self, status: int, data: object, extra: dict[str, str] | None = None) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _peer(self) -> PeerEntry | None:
        header = self.headers.get("Authorization") or ""
        scheme, _, supplied = header.partition(" ")
        return self.config.authenticate(supplied.strip()) if scheme.lower() == "bearer" else None

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == CARD_PATH:
            description = self.profile.description() if self.profile is not None else None
            name = self.profile.name() if self.profile is not None else None
            identity = self.profile.identity() if self.profile is not None else None
            self._send(200, agent_card(self.config, description, name, identity), {"Cache-Control": "max-age=30"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        # Read the body before refusing, so the client sees the status instead of a reset connection.
        body = self.rfile.read(length) if 0 <= length <= MAX_BODY_BYTES else None
        if self.path.split("?")[0] != RPC_PATH:
            self._send(404, {"error": "not found"})
            return
        peer = self._peer()
        if peer is None:
            self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            return
        try:
            request = json.loads(body.decode("utf-8")) if body is not None else None
        except (ValueError, RecursionError):
            request = None
        method = request.get("method") if isinstance(request, dict) else None
        # Following an accepted task must not exhaust the budget for asking questions (or vice versa).
        # Keep all queries bounded, including queries for unknown or another peer's task IDs.
        budget = "task-status" if method == "GetTask" else "task-cancel" if method == "CancelTask" else "requests"
        limit = max(peer.rate_per_min, TASK_POLL_RATE_PER_MIN) if budget == "task-status" else peer.rate_per_min
        if not self.limiter.allow(peer.name, limit, budget=budget):
            self._send(429, {"error": "rate limit"}, {"Retry-After": "60"})
            return
        if (self.headers.get("A2A-Version") or "").strip() != PROTOCOL_VERSION:
            self._send(200, {"jsonrpc": "2.0", "id": None, "error": {
                "code": VERSION_NOT_SUPPORTED, "message": f"only A2A-Version {PROTOCOL_VERSION} is supported"}})
            return
        if body is None or request is None:
            code = PARSE_ERROR if body is not None else INVALID_REQUEST
            self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": "invalid request"}})
            return
        try:
            self._send(200, self.dispatcher.handle(peer, request))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception("a2a request failed")
            self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal error"}})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("a2a: " + format, *args)


def start_server(
    config: A2aConfig, dispatcher: Dispatcher, limiter: RateLimiter | None = None, profile: ProfileCache | None = None
) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {
        "config": config, "dispatcher": dispatcher, "limiter": limiter or RateLimiter(), "profile": profile,
    })
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="a2a-http", daemon=True).start()
    return server
