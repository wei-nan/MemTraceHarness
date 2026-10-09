"""A local door for scripts that want to say something through this bot.

A cron job or an analysis script that used to read the Harness's `.env` for a bot token now posts
here instead and never holds the token:

    POST /v1/send        {"text": "..."}                         -> {"ok": true, "message_ids": [...]}
    POST /v1/send_photo  {"path": "/abs/file.png", "caption"?}  -> {"ok": true, "message_ids": [...]}

Loopback only, bearer-token protected (TELEGRAM_RELAY_TOKEN), and a photo is only read from inside
TELEGRAM_RELAY_PHOTO_ROOTS (none configured = photos refused). Messages go to every allowlisted chat.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from memtrace_services.telegram.service import TelegramService

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Telegram's own limit for photos


class _Handler(BaseHTTPRequestHandler):
    service: TelegramService
    token: str

    def _send(self, status: int, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        try:  # (the body is read before anything is refused, so the client sees the refusal)
            length = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8")) if 0 < length <= 1_000_000 else {}
        except Exception:
            body = None
        scheme, _, supplied = (self.headers.get("Authorization") or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), self.token):
            self._send(401, {"error": "unauthorized"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "malformed request body"})
            return
        chats = sorted(self.service.settings.allowed_chat_ids)
        if self.path.rstrip("/") == "/v1/send":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send(400, {"error": "text is required"})
                return
            ids = [self.service.bot.send_text(chat, text) for chat in chats]
        elif self.path.rstrip("/") == "/v1/send_photo":
            try:
                photo = self._read_photo(body.get("path"))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
                return
            caption = body.get("caption") if isinstance(body.get("caption"), str) else None
            ids = [self.service.bot.send_photo(chat, photo[1], photo[0], caption) for chat in chats]
        else:
            self._send(404, {"error": "not found"})
            return
        ok = all(i is not None for i in ids)
        self._send(200 if ok else 502, {"ok": ok, "message_ids": [i for i in ids if i is not None]})

    def _read_photo(self, raw_path: object) -> tuple[str, bytes]:
        roots = self.service.settings.relay_photo_roots
        if not roots:
            raise ValueError("photos are disabled (TELEGRAM_RELAY_PHOTO_ROOTS is empty)")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path).resolve()
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError("path is outside TELEGRAM_RELAY_PHOTO_ROOTS")
        if not path.is_file():
            raise ValueError("file does not exist")
        if path.stat().st_size > MAX_PHOTO_BYTES:
            raise ValueError("file is larger than 10 MB")
        return path.name, path.read_bytes()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("telegram relay: " + format, *args)


def start_relay(service: TelegramService) -> ThreadingHTTPServer | None:
    settings = service.settings
    if settings.relay_port is None:
        return None
    if not settings.relay_token:
        raise ValueError("TELEGRAM_RELAY_PORT is set but TELEGRAM_RELAY_TOKEN is not")
    handler = type("RelayHandler", (_Handler,), {"service": service, "token": settings.relay_token})
    server = ThreadingHTTPServer(("127.0.0.1", settings.relay_port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="telegram-relay", daemon=True).start()
    logger.info(f"relay listening on http://127.0.0.1:{settings.relay_port}")
    return server
