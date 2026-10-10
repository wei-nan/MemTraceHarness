"""The Harness's local HTTP API: the only door a service (Telegram, the status page, a script) uses.

Plain stdlib HTTP on loopback with a bearer token. A service never reads the Harness's database and
the Harness never calls a service: people's words come in through POST /v1/messages and
POST /v1/actions, and what the Harness wants said goes out through the outbox, which a service
long-polls with GET /v1/events and acknowledges with POST /v1/events/<id>/ack.

    GET  /v1/health                       liveness (no token needed)
    GET  /v1/commands                     slash commands this Harness understands
    POST /v1/messages                     {text, audience, reply_to_ref?, reply_to_text?} -> 202
    POST /v1/actions                      {id, audience, message_ref?} -> {toast}
    GET  /v1/events?limit=&wait=          undelivered outbox events (long-poll)
    POST /v1/events/<id>/ack              {ok, message_refs?}
    GET  /v1/status                       the status description (admin.collect_status_data)
    GET  /v1/changes?since=&wait=         long-poll: returns when something worth redrawing happened
    GET  /v1/turns/<id>                   one stage attempt's full detail
    POST /v1/admin/<setting>              change a model setting / resolve an approval or preference
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from memtrace_harness import admin

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.service import HarnessService

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
MAX_WAIT_SECONDS = 55.0


class ChangeBus:
    """Version counter + condition variable, not a per-client queue: every long-poll just waits
    for the version to change, then recomputes the latest status itself. A burst of publishes costs
    each client one recomputation, not one per publish."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._version = 0

    @property
    def version(self) -> int:
        return self._version

    def publish(self) -> None:
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait_for_update(self, last_seen: int, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._version != last_seen, timeout=timeout)
            return self._version


class MessageWorker:
    """Handles people's messages one after another, in the order they arrived: a chat reply can
    take a minute, and the API call that delivered the message must not wait for it."""

    def __init__(self, service: HarnessService, bus: ChangeBus) -> None:
        self._service = service
        self._bus = bus
        self._queue: queue.Queue[tuple[str, dict] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="harness-messages", daemon=True)
        self._thread.start()

    def submit(self, payload: dict) -> None:
        self._queue.put(("message", payload))

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            _, payload = item
            try:
                self._service.handle_message(
                    payload["text"],
                    audience=payload.get("audience"),
                    reply_to_ref=payload.get("reply_to_ref"),
                    reply_to_text=payload.get("reply_to_text"),
                )
            except Exception:
                logger.exception("handling a message failed")
                try:
                    self._service.send_message(payload.get("audience"), "⚠️ 處理這則訊息時發生未預期錯誤，請查看日誌。")
                except Exception:
                    logger.exception("reporting the failure failed")
            self._bus.publish()

    def stop(self) -> None:
        self._queue.put(None)


def _json_body(handler: BaseHTTPRequestHandler) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        return None
    if length > MAX_BODY_BYTES:
        return None
    raw = handler.rfile.read(length) if length else b""
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


class _Handler(BaseHTTPRequestHandler):
    api: "HarnessApi"

    # ---- plumbing
    def _send(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized(self) -> bool:
        token = self.api.token
        if not token:
            return False
        header = self.headers.get("Authorization") or ""
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip(), token)

    def _query(self) -> tuple[str, dict[str, str]]:
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        return parsed.path.rstrip("/") or "/", query

    @staticmethod
    def _float(query: dict[str, str], key: str, default: float, upper: float) -> float:
        try:
            return max(0.0, min(float(query.get(key, default)), upper))
        except ValueError:
            return default

    # ---- routes
    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path, query = self._query()
        if path == "/v1/health":
            self._send(200, {"ok": True, "project": self.api.scope.name, "pid": os.getpid()})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            if path == "/v1/commands":
                self._send(200, {"commands": self.api.service.commands()})
            elif path == "/v1/events":
                limit = int(self._float(query, "limit", 20, 100))
                wait = self._float(query, "wait", 0, MAX_WAIT_SECONDS)
                self._send(200, {"events": self.api.service.fetch_events(limit=limit, wait_seconds=wait)})
            elif path == "/v1/status":
                self._send(200, self.api.status())
            elif path == "/v1/changes":
                since = int(self._float(query, "since", -1, 1e12))
                wait = self._float(query, "wait", 0, MAX_WAIT_SECONDS)
                version = self.api.bus.wait_for_update(since, wait) if wait else self.api.bus.version
                self._send(200, {"version": version})
            elif path.startswith("/v1/turns/"):
                detail = admin.collect_turn_detail(self.api.config, int(path.rsplit("/", 1)[1]))
                self._send(200 if detail is not None else 404, detail or {"error": "not found"})
            else:
                self._send(404, {"error": "not found"})
        except ValueError:
            self._send(400, {"error": "malformed request"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.exception(f"GET {path} failed")
            self._send(500, {"error": "internal error"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib method name)
        path, _ = self._query()
        # The body is read before anything is refused: answering and closing with it unread resets
        # the client's connection instead of letting it see the 401/404.
        body = _json_body(self)
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        if body is None:
            self._send(400, {"error": "malformed request body"})
            return
        try:
            if path == "/v1/messages":
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    self._send(400, {"error": "text is required"})
                    return
                self.api.worker.submit(
                    {
                        "text": text,
                        "audience": _opt_str(body.get("audience")),
                        "reply_to_ref": _opt_str(body.get("reply_to_ref")),
                        "reply_to_text": _opt_str(body.get("reply_to_text")),
                    }
                )
                self._send(202, {"accepted": True})
            elif path == "/v1/actions":
                action_id = body.get("id")
                if not isinstance(action_id, str) or not action_id:
                    self._send(400, {"error": "id is required"})
                    return
                toast = self.api.service.handle_action(
                    action_id,
                    audience=_opt_str(body.get("audience")),
                    message_ref=_opt_str(body.get("message_ref")),
                )
                self.api.bus.publish()
                self._send(200, {"toast": toast})
            elif path.startswith("/v1/events/") and path.endswith("/ack"):
                event_id = int(path.split("/")[3])
                refs = body.get("message_refs")
                result = self.api.service.ack_event(
                    event_id,
                    ok=bool(body.get("ok")),
                    message_refs=[str(r) for r in refs] if isinstance(refs, list) else None,
                )
                self._send(200, {"result": result})
            elif path.startswith("/v1/admin/"):
                self._admin(path[len("/v1/admin/"):], body)
            else:
                self._send(404, {"error": "not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception:
            logger.exception(f"POST {path} failed")
            self._send(500, {"error": "internal error"})

    def _admin(self, setting: str, body: dict) -> None:
        config = self.api.config
        try:
            if setting == "role-profile":
                admin.update_role_profile(config, str(body["profile_id"]), str(body["provider"]), str(body["model"]))
            elif setting == "role-profile-chain":
                admin.update_role_profile_chain(config, str(body["profile_id"]), body["chain"])
            elif setting in ("chat-chain", "digest-chain", "archive-chain", "recall-chain"):
                admin.update_model_chain(config, setting.split("-")[0], body["chain"])
            elif setting == "model-catalog-refresh":
                admin.refresh_model_catalog()
            elif setting == "role-profile-independent":
                dest = admin.create_dedicated_role_profiles_file(config)
                self.api.bus.publish()
                self._send(200, {"ok": True, "path": str(dest)})
                return
            elif setting == "chat-model":
                admin.update_chat_model(config, str(body["provider"]), str(body["model"]))
            elif setting == "digest-model":
                admin.update_digest_model(config, str(body["provider"]), str(body["model"]))
            elif setting == "digest-fallbacks":
                admin.update_digest_fallbacks(config, str(body["fallbacks"]))
            elif setting == "recall-model":
                admin.update_recall_model(config, str(body["provider"]), str(body["model"]))
            elif setting == "recall-fallbacks":
                admin.update_recall_fallbacks(config, str(body["fallbacks"]))
            elif setting == "approval":
                self._resolve_approval(body)
                return
            elif setting == "preference":
                self._resolve_preference(body)
                return
            elif setting == "stuck":
                self._resolve_stuck(body)
                return
            else:
                self._send(404, {"error": "not found"})
                return
        except (KeyError, TypeError):
            self._send(400, {"error": "malformed request body"})
            return
        self.api.bus.publish()
        self._send(200, {"ok": True})

    def _resolve_approval(self, body: dict) -> None:
        try:
            request_id = str(body["request_id"])
            action = str(body["action"]).lower()
        except (KeyError, TypeError):
            self._send(400, {"error": "malformed request body"})
            return
        if action not in ("approve", "reject", "clarify", "choose"):
            self._send(400, {"error": f"unsupported action {action!r} (expected approve, reject, clarify or choose)"})
            return
        service = self.api.service
        if service.approval_manager.get_request(request_id) is None:
            self._send(400, {"error": f"approval request {request_id!r} not found"})
            return
        # Same code path a text command, a button or a reply goes through — one place resolves an
        # approval, this is just another door to it. A resulting Agent Loop resumption runs in a
        # background thread, so this returns at once.
        if action == "clarify":
            answer = body.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                self._send(400, {"error": "clarify needs the answer text"})
                return
            msg = service.resolve_approval(request_id, "clarify", answer=answer.strip())
        elif action == "choose":
            choice = body.get("choice")
            if not isinstance(choice, int) or isinstance(choice, bool):
                self._send(400, {"error": "choose needs the option number"})
                return
            msg = service.resolve_card_pick(request_id, choice)
        else:
            msg = service.resolve_approval(request_id, action)
        self.api.bus.publish()
        self._send(200, {"ok": True, "message": msg})

    def _resolve_stuck(self, body: dict) -> None:
        """Give up an interrupted task: mark it failed and free its workspace slot. The status page asks
        the operator to confirm first; a task that is really running cannot be abandoned this way."""
        from memtrace_harness.inflight import default_tracker

        conversation_id = body.get("conversation_id")
        if not isinstance(conversation_id, str) or body.get("action") != "abandon":
            self._send(400, {"error": "malformed request body"})
            return
        if any(entry["conversation_id"] == conversation_id for entry in default_tracker.snapshot()):
            self._send(400, {"error": "這個任務正在執行中，不能放棄"})
            return
        trace_store = self.api.service.approval_manager.trace_store
        if not trace_store.abandon_conversation(self.api.scope.workspace_id, conversation_id):
            self._send(400, {"error": "找不到這個未完成的任務"})
            return
        self.api.bus.publish()
        self._send(200, {"ok": True, "message": f"已放棄 {conversation_id}，工作區已釋放。"})

    def _resolve_preference(self, body: dict) -> None:
        from memtrace_harness.memory_digest import resolve_preference, sync_operator_profile
        from memtrace_harness.memtrace_client import MemTraceClient

        try:
            rule_id = int(body["id"])
            action = str(body["action"]).lower()
        except (KeyError, TypeError, ValueError):
            self._send(400, {"error": "malformed request body"})
            return
        text, scope = body.get("text"), body.get("scope")
        if (text is not None and not isinstance(text, str)) or (scope is not None and not isinstance(scope, str)):
            self._send(400, {"error": "malformed request body"})
            return
        config = self.api.config
        trace_store = self.api.service.approval_manager.trace_store
        rule = resolve_preference(trace_store, rule_id, action, text=text, scope=scope)
        # Local SQLite is what chat reads, so the change is already in effect. Rewriting the
        # MemTrace profile node is a best-effort mirror.
        synced: bool | None = None
        if action in ("adopt", "retire", "restore") and config.memtrace_mcp_url and config.operator_preference_workspace_id:
            try:
                client = MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token)
                sync_operator_profile(trace_store, client, config.operator_preference_workspace_id)
                synced = True
            except Exception:
                logger.exception("mirroring the operator profile to MemTrace failed")
                synced = False
        self.api.bus.publish()
        self._send(200, {"ok": True, "rule": rule, "memtrace_synced": synced})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("harness api: " + format, *args)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class HarnessApi:
    def __init__(
        self,
        service: HarnessService,
        config: HarnessConfig,
        scope: ProjectScope,
        *,
        bus: ChangeBus | None = None,
        started_at: str | None = None,
    ) -> None:
        self.service = service
        self.config = config
        self.scope = scope
        self.token = config.api_token
        self.bus = bus or ChangeBus()
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.worker = MessageWorker(service, self.bus)
        self._server: ThreadingHTTPServer | None = None

    def status(self) -> dict:
        return admin.collect_status_data(self.config, self.scope, self.started_at)

    def start(self) -> ThreadingHTTPServer:
        if not self.token:
            raise RuntimeError(
                "HARNESS_API_TOKEN is not set in .harness.env: the API refuses to start without a token"
            )
        handler = type("BoundHandler", (_Handler,), {"api": self})
        server = ThreadingHTTPServer((self.config.api_host, self.config.api_port), handler)
        server.daemon_threads = True  # long-polls must not keep the process alive at shutdown
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="harness-api", daemon=True
        ).start()
        self._server = server
        logger.info(f"Harness API listening on http://{self.config.api_host}:{self.config.api_port}")
        return server

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        self.worker.stop()
