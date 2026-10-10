"""The status web service's HTTP server."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memtrace_services.client import HarnessError, HarnessUnavailable
from memtrace_services.web.aggregate import HEARTBEAT_SECONDS, Aggregator
from memtrace_services.web.page import PAGE_TEMPLATE

logger = logging.getLogger(__name__)

# The page's write endpoints -> the Harness API setting each one forwards to.
ADMIN_ROUTES = {
    "/api/role-profile": "role-profile",
    "/api/role-profile/independent": "role-profile-independent",
    "/api/role-profile-chain": "role-profile-chain",
    "/api/chat-chain": "chat-chain",
    "/api/digest-chain": "digest-chain",
    "/api/archive-chain": "archive-chain",
    "/api/recall-chain": "recall-chain",
    "/api/model-catalog-refresh": "model-catalog-refresh",
    "/api/chat-model": "chat-model",
    "/api/recall-model": "recall-model",
    "/api/recall-fallbacks": "recall-fallbacks",
    "/api/digest-fallbacks": "digest-fallbacks",
    "/api/digest-model": "digest-model",
    "/api/approval": "approval",
    "/api/preference": "preference",
}


class _Handler(BaseHTTPRequestHandler):
    aggregator: Aggregator

    def _json(self, status: int, data) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/", ""):
            page = PAGE_TEMPLATE.replace("__INITIAL_DATA__", json.dumps(self.aggregator.collect()))
            payload = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/events":
            self._events()
        elif parsed.path == "/api/turn":
            self._turn(urllib.parse.parse_qs(parsed.query))
        else:
            self.send_response(404)
            self.end_headers()

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_seen = 0
        try:
            while True:
                frame = f"data: {json.dumps(self.aggregator.collect())}\n\n".encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
                last_seen = self.aggregator.changes.wait(last_seen, HEARTBEAT_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client navigated away or closed the tab — not an error

    def _turn(self, query: dict[str, list[str]]) -> None:
        # On-demand only: artifact/final_text bodies can be long and most stages are never clicked.
        try:
            turn_id = int((query.get("turn_id") or [""])[0])
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        target = self.aggregator.target_for((query.get("project") or [""])[0])
        if target is None:
            self._json(404, None)
            return
        try:
            detail = target.client().turn(turn_id)
        except (HarnessUnavailable, HarnessError):
            detail = None
        self._json(200 if detail is not None else 404, detail)

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        # The write actions this page offers. Reachable only where the page itself is: bound to
        # loopback unless the operator widens it, and there is no separate login — widening
        # exposure widens who can change a project's models or approve its pending work too.
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            body = None
        setting = ADMIN_ROUTES.get(path)  # (body first: see api.py)
        if setting is None:
            self._json(404, {"error": "not found"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("project"), str):
            self._json(400, {"error": "malformed request body"})
            return
        target = self.aggregator.target_for(body["project"])
        if target is None:
            self._json(400, {"error": f"no Harness serves project {body['project']!r}"})
            return
        try:
            result = target.client().admin(setting, {k: v for k, v in body.items() if k != "project"})
        except HarnessError as exc:
            self._json(400 if exc.status < 500 else 502, {"error": exc.message})
            return
        except HarnessUnavailable as exc:
            self._json(502, {"error": f"Harness unreachable: {exc}"})
            return
        self.aggregator.changes.publish()
        self._json(200, result)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("status web: " + format, *args)


def start_server(host: str, port: int, aggregator: Aggregator) -> ThreadingHTTPServer:
    handler = type("StatusHandler", (_Handler,), {"aggregator": aggregator})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True  # long-lived SSE connections must not keep the process alive
    threading.Thread(target=server.serve_forever, name="status-web", daemon=True).start()
    return server
