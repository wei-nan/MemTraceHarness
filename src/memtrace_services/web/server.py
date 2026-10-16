"""The status web service's HTTP server."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from memtrace_services.client import HarnessError, HarnessUnavailable
from memtrace_services.web.aggregate import HEARTBEAT_SECONDS, Aggregator
from memtrace_services.web.chat import ChatHub
from memtrace_services.web.page import STATIC_DIR, STATIC_TYPES, render_page
from memtrace_services.web import pairing

logger = logging.getLogger(__name__)

# The page's write endpoints -> the Harness API setting each one forwards to.
ADMIN_ROUTES = {
    "/api/role-profile": "role-profile",
    "/api/role-profile/independent": "role-profile-independent",
    "/api/role-profile-chain": "role-profile-chain",
    "/api/chat-chain": "chat-chain",
    "/api/digest-chain": "digest-chain",
    "/api/archive-chain": "archive-chain",
    "/api/audit-chain": "audit-chain",
    "/api/recall-chain": "recall-chain",
    "/api/memory-feature-enabled": "memory-feature-enabled",
    "/api/model-catalog-refresh": "model-catalog-refresh",
    "/api/chat-model": "chat-model",
    "/api/recall-model": "recall-model",
    "/api/recall-fallbacks": "recall-fallbacks",
    "/api/digest-fallbacks": "digest-fallbacks",
    "/api/digest-model": "digest-model",
    "/api/approval": "approval",
    "/api/stuck": "stuck",
    "/api/chat-question": "chat-question",
    "/api/preference": "preference",
    "/api/shared-memory": "shared-memory",
    "/api/execution-source": "execution-source",
    "/api/execution-source-allowlist": "execution-source-allowlist",
    "/api/improvement": "improvement",
    "/api/peer-description": "peer-description",
    "/api/peer-name": "peer-name",
    # Pairing is not here: making a link returns its token, which must not reach the browser (pairing.py does both halves).
    "/api/peer-link-trust": "peer-link-inbound-trust",
    "/api/peer-link-revoke": "peer-link-inbound-revoke",
    "/api/peer-link-outbound-flag": "peer-link-outbound-collaborate",
    "/api/peer-link-outbound-remove": "peer-link-outbound-remove",
}


class _Handler(BaseHTTPRequestHandler):
    aggregator: Aggregator
    chat: ChatHub

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
            page = render_page(json.dumps(self.aggregator.collect()))
            payload = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path.startswith("/static/") and parsed.path[len("/static/"):] in STATIC_TYPES:
            name = parsed.path[len("/static/"):]
            payload = (STATIC_DIR / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", STATIC_TYPES[name])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif parsed.path == "/events":
            self._events()
        elif parsed.path == "/api/turn":
            self._turn(urllib.parse.parse_qs(parsed.query))
        elif parsed.path == "/api/chat":
            self._chat_poll(urllib.parse.parse_qs(parsed.query))
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

    def _chat_target(self, project: str) -> str | None:
        target = self.aggregator.target_for(project)
        return target.name if target is not None and self.chat.known(target.name) else None

    def _chat_poll(self, query: dict[str, list[str]]) -> None:
        harness = self._chat_target((query.get("project") or [""])[0])
        if harness is None:
            self._json(404, {"error": "no Harness serves this project"})
            return
        try:
            since = int((query.get("since") or ["0"])[0])
            wait = max(0.0, min(float((query.get("wait") or ["0"])[0]), 25.0))
        except ValueError:
            self._json(400, {"error": "malformed request"})
            return
        try:
            self._json(200, self.chat.poll(harness, since, wait))
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        if path in ("/api/chat-send", "/api/chat-action"):
            self._chat_post(path, body)
            return
        if path in ("/api/peer-pair", "/api/peer-invite", "/api/peer-accept"):
            self._pairing_post(path, body)
            return
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

    def _pairing_post(self, path: str, body) -> None:
        if not isinstance(body, dict):
            self._json(400, {"error": "malformed request body"})
            return
        text = lambda key: body.get(key) if isinstance(body.get(key), str) else ""  # noqa: E731
        try:
            if path == "/api/peer-pair":
                result = pairing.pair(
                    self.aggregator, text("from"), text("to"), text("trust"),
                    both=body.get("both") is True, reverse_trust=text("reverse_trust") or "ask",
                )
            elif path == "/api/peer-invite":
                result = {"invite": pairing.make_invite(self.aggregator, text("project"), text("name"), text("trust"))}
            else:
                result = pairing.accept_invite(self.aggregator, text("project"), text("invite"))
        except pairing.PairingError as exc:
            self._json(400, {"error": str(exc)})
            return
        self.aggregator.changes.publish()
        self._json(200, result)

    def _chat_post(self, path: str, body) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("project"), str):
            self._json(400, {"error": "malformed request body"})
            return
        harness = self._chat_target(body["project"])
        if harness is None:
            self._json(400, {"error": f"no Harness serves project {body['project']!r}"})
            return
        try:
            if path == "/api/chat-send":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    self._json(400, {"error": "text is required"})
                    return
                self.chat.send(harness, text.strip())
                self._json(202, {"accepted": True})
            else:
                action_id, ref = body.get("id"), body.get("ref")
                if not isinstance(action_id, str) or not action_id:
                    self._json(400, {"error": "id is required"})
                    return
                toast = self.chat.act(harness, action_id, ref if isinstance(ref, str) else None)
                self.aggregator.changes.publish()
                self._json(200, {"toast": toast})
        except HarnessError as exc:
            self._json(400 if exc.status < 500 else 502, {"error": exc.message})
        except HarnessUnavailable as exc:
            self._json(502, {"error": f"Harness unreachable: {exc}"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("status web: " + format, *args)


def start_server(host: str, port: int, aggregator: Aggregator, chat: ChatHub) -> ThreadingHTTPServer:
    handler = type("StatusHandler", (_Handler,), {"aggregator": aggregator, "chat": chat})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True  # long-lived SSE connections must not keep the process alive
    threading.Thread(target=server.serve_forever, name="status-web", daemon=True).start()
    return server
