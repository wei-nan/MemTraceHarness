"""Loopback read-only gateway to TaiwanTrade that injects the API key on the Harness side.

A scheduled job is a script run by a model CLI, and that CLI's environment is an allowlist
(``cli_process.child_env``) that deliberately carries no secrets. The script therefore cannot
send ``X-API-KEY`` itself. Instead of widening the allowlist, the Harness serves this gateway on
127.0.0.1: the script points its base URL here, the key is read from
``HARNESS_TAIWANTRADE_API_KEY_FILE`` per request and added to the upstream call, and the key never
enters a model-visible environment, command line or log.

The same door carries one outbound thing: ``POST /notify/photo {"path", "caption"?}`` asks the Harness to put an
image from the workspace in its outbox, and the Telegram service (which holds the bot token) sends it. A script
that used to read a bot token itself therefore needs no token at all.

Only GET is forwarded, and only to the paths of the read-only tools in ``taiwantrade_mcp.TOOLS``
plus the TAIEX chart. Anything else (orders, watchlist writes, other methods) is refused here.
Opt-in is ``HARNESS_TAIWANTRADE_GATEWAY_PORT``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from memtrace_harness.taiwantrade_mcp import DEFAULT_BASE_URL, TOOLS, ProxyError, load_api_key

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"
_SEGMENT = r"[0-9A-Za-z_,-]{1,64}"
_EXTRA_PATHS = ("/charts/taiex",)
REQUEST_TIMEOUT_SECONDS = 30


def _path_pattern(path: str) -> re.Pattern[str]:
    parts = re.split(r"\{[a-z_]+\}", path)
    return re.compile("^" + _SEGMENT.join(re.escape(part) for part in parts) + "$")


_ALLOWED = tuple(_path_pattern(path) for path in [tool.path for tool in TOOLS] + list(_EXTRA_PATHS))


def is_allowed(path: str) -> bool:
    """True for a GET path (relative to ``/api/v1``) that is one of the read-only endpoints."""
    return any(pattern.match(path) for pattern in _ALLOWED)


def _upstream_origin(base_url: str) -> str:
    """``http://host:port`` of the TaiwanTrade API; the gateway re-adds ``/api/v1`` itself."""
    parsed = urllib.parse.urlsplit(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg")
MAX_PHOTO_BYTES = 10 * 1024 * 1024


def photo_notifier(workspace: Path, enqueue: Callable[..., int]) -> Callable[[object, object], int]:
    """A callable that queues one workspace image for the operator, refusing anything else."""
    root = workspace.resolve()

    def notify(raw_path: object, caption: object) -> int:
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path).resolve()
        if root not in path.parents or path.suffix.lower() not in PHOTO_SUFFIXES:
            raise ValueError("path must be a png/jpg inside the workspace")
        if not path.is_file() or path.stat().st_size > MAX_PHOTO_BYTES:
            raise ValueError("file is missing or larger than 10 MB")
        text = caption.strip()[:1024] if isinstance(caption, str) and caption.strip() else None
        return enqueue("photo", text=text, ref=str(path))

    return notify


def build_handler(
    env: dict[str, str],
    opener: Callable[..., Any] = urllib.request.urlopen,
    notify_photo: Callable[[object, object], int] | None = None,
) -> type[BaseHTTPRequestHandler]:
    origin = _upstream_origin(env.get("HARNESS_TAIWANTRADE_API_URL") or DEFAULT_BASE_URL)

    class Handler(BaseHTTPRequestHandler):
        server_version = "taiwantrade-gateway"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - no request log: paths carry symbols only
            return

        def _reply(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            split = urllib.parse.urlsplit(self.path)
            if not split.path.startswith(API_PREFIX + "/") or not is_allowed(split.path[len(API_PREFIX):]):
                self._reply(403, b"path is not a read-only TaiwanTrade endpoint")
                return
            try:
                key = load_api_key(env)
            except ProxyError as exc:
                self._reply(502, str(exc).encode())
                return
            url = origin + split.path + (f"?{split.query}" if split.query else "")
            request = urllib.request.Request(url, headers={"X-API-KEY": key} if key else {}, method="GET")
            try:
                with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    self._reply(
                        response.status,
                        response.read(),
                        response.headers.get("Content-Type", "application/octet-stream"),
                    )
            except urllib.error.HTTPError as exc:
                self._reply(exc.code, exc.read()[:2000], exc.headers.get("Content-Type", "text/plain"))
            except (urllib.error.URLError, OSError) as exc:
                self._reply(502, f"cannot reach TaiwanTrade: {getattr(exc, 'reason', exc)}".encode())

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length) if 0 < length <= 100_000 else b""
            if self.path.rstrip("/") != "/notify/photo" or notify_photo is None:
                self._reply(405, b"the gateway only forwards reads and queues photos")
                return
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                event_id = notify_photo(body.get("path"), body.get("caption"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._reply(400, str(exc).encode())
                return
            self._reply(202, json.dumps({"queued": event_id}).encode(), "application/json")

        def _refuse(self) -> None:
            self._reply(405, b"the gateway is read-only")

        do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

    return Handler


def start_gateway(
    env: dict[str, str],
    opener: Callable[..., Any] = urllib.request.urlopen,
    notify_photo: Callable[[object, object], int] | None = None,
) -> ThreadingHTTPServer | None:
    """Start the gateway when the operator opted in (key file + port); otherwise return None."""
    port = env.get("HARNESS_TAIWANTRADE_GATEWAY_PORT", "").strip()
    if not port or not env.get("HARNESS_TAIWANTRADE_API_KEY_FILE"):
        return None
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), build_handler(env, opener, notify_photo))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="taiwantrade-gateway", daemon=True).start()
    logger.info(f"TaiwanTrade gateway listening on http://127.0.0.1:{port} (read-only, key held by the Harness)")
    return server
