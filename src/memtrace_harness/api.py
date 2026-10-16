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

    A second, narrower token (HARNESS_PEER_API_TOKEN) opens only these, for the A2A service relaying
    another Harness's read-only question (peer.py, docs/a2a-peers.md); the main token does not open them:
    POST /v1/peer/requests                {peer, context_id, text, skill, hops} -> 202 {request}
    GET  /v1/peer/profile                 {name, description}: what this Harness is called and says about itself (status page settings)
    GET  /v1/peer/requests?peer=&context_id=        that peer's requests
    GET  /v1/peer/requests/<id>?peer=     one request, 404 for anything not that peer's
    POST /v1/peer/requests/<id>/cancel    {peer}
and the other direction, questions this Harness asks (peer.py PeerOutbound):
    GET  /v1/peer/outbound?wait=          queued questions to carry (long-poll), marked sent
    POST /v1/peer/outbound/<id>/result    {state: completed|failed, text?, reason?}
    POST /v1/peer/directory               {peers: [{name, description}]} the peers the service can reach
    GET  /v1/peer/links                   who may call this Harness (token hashes) and whom it may call (tokens): peer_links.py
    POST /v1/peer/endpoint                {url} the A2A service's own public address
and, for the status page with the main token: GET /v1/peer-links (the same without any secret), and
POST /v1/admin/peer-link-<inbound-create|inbound-trust|inbound-revoke|outbound-set|outbound-collaborate|outbound-remove>
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

from memtrace_harness import admin, attachments
from memtrace_harness.cli_process import kill_process_for_thread
from memtrace_harness.peer import PeerAskService, PeerOutbound

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.service import HarnessService

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_000_000
MAX_MESSAGE_BODY_BYTES = 60_000_000  # a message may carry files, base64 (attachments.py)
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
    take a minute, and the API call that delivered the message must not wait for it.

    A plain chat message is handled at once. If the same person sends another plain chat message
    before the reply has gone out, the reply being made is cancelled (a model call in flight is
    killed, a finished-but-unsent reply is dropped — nothing in either has been acted on) and the
    model thinks again with everything said so far, so they get one answer. A command, a reply to a
    specific message, or a message from someone else is never merged; it waits its turn. After
    MAX_RESTARTS restarts for one burst the worker stops interrupting and lets the reply through."""

    MAX_RESTARTS = 3
    _PROGRESS_QUERIES = frozenset({
        "有在處理嗎", "還在處理嗎", "有在跑嗎", "還在嗎", "進度如何", "目前進度", "現在進度",
    })

    def __init__(self, service: HarnessService, bus: ChangeBus) -> None:
        self._service = service
        self._bus = bus
        self._cond = threading.Condition()
        self._inbox: list[dict] = []
        self._stopped = False
        self._current: dict | None = None  # the batch being answered, when it may still be redone
        self._superseded = False
        self._cancelled = False  # /cancel arrived while a chat reply was being made: drop it, do not redo it
        service.chat_superseded = lambda: self._superseded
        self._thread = threading.Thread(target=self._run, name="harness-messages", daemon=True)
        self._thread.start()

    def submit(self, payload: dict) -> None:
        with self._cond:
            # A status-only follow-up must not throw away minutes of ongoing lookup work.
            if (
                self._current is not None
                and not self._superseded
                and self._service.chat_generating
                and payload.get("audience") == self._current.get("audience")
                and not any(payload.get(k) for k in ("reply_to_ref", "reply_to_text", "attachments"))
                and (payload.get("text") or "").strip().rstrip("？?！!。.") in self._PROGRESS_QUERIES
            ):
                self._service.report_chat_wait(payload["text"], payload.get("audience"))
                self._bus.publish()
                return
            self._inbox.append(payload)
            interrupt = (
                self._current is not None
                and not self._superseded
                and payload.get("audience") == self._current.get("audience")
                and self._is_mergeable(payload)
            )
            if not interrupt and self._current is not None and not self._superseded and self._is_cancel(payload):
                interrupt = True
                self._cancelled = True
            if interrupt:
                self._superseded = True
            self._cond.notify_all()
        if interrupt and self._service.chat_generating:
            kill_process_for_thread(self._thread.ident)

    @staticmethod
    def _is_cancel(payload: dict) -> bool:
        return (payload.get("text") or "").strip().lower().split("@")[0] in {"/cancel", "/stop"}

    def _is_mergeable(self, payload: dict) -> bool:
        if payload.get("reply_to_ref") or payload.get("reply_to_text"):
            return False
        text = (payload.get("text") or "").strip()
        if not text or text.startswith("/"):
            return False
        try:
            return self._service.triage.triage_message(text).kind == "chat"
        except Exception:
            return False

    def _take_mergeable(self, audience: object) -> list[dict]:
        """Every plain chat message from `audience` that is waiting, in order; the rest stay queued."""
        with self._cond:
            taken = [p for p in self._inbox if p.get("audience") == audience and self._is_mergeable(p)]
            for p in taken:
                self._inbox.remove(p)
            return taken

    def _handle(self, payload: dict, earlier_text: str = "") -> None:
        self._service.handle_message(
            payload["text"],
            audience=payload.get("audience"),
            reply_to_ref=payload.get("reply_to_ref"),
            reply_to_text=payload.get("reply_to_text"),
            earlier_text=earlier_text,
            attachments=payload.get("attachments") or None,
        )

    def _answer(self, first: dict) -> None:
        from memtrace_harness.service import ChatInterrupted

        with self._cond:
            self._cancelled = False
            self._superseded = False
        if not self._is_mergeable(first):
            self._handle(first)
            return
        audience = first.get("audience")
        earlier = ""
        current = first
        restarts = 0
        while True:
            with self._cond:
                self._superseded = False
                self._current = current if restarts < self.MAX_RESTARTS else None
            waiting = self._take_mergeable(audience)
            if waiting:
                current = {
                    **waiting[-1],
                    "text": "\n\n".join([current["text"].strip(), *(p["text"].strip() for p in waiting)]),
                    "attachments": [*(current.get("attachments") or []), *(a for p in waiting for a in p.get("attachments") or [])],
                }
            try:
                self._handle(current, earlier)
                return
            except ChatInterrupted:
                if self._cancelled:
                    self._cancelled = False
                    return  # the /cancel waiting in the inbox is handled next; this reply is not made
                more = self._take_mergeable(audience)
                if not more:
                    # The newcomer was claimed already or is gone: carry on with what we had.
                    restarts = self.MAX_RESTARTS
                    continue
                restarts += 1
                logger.info(f"answering {1 + len(more)} chat messages as one (restart {restarts})")
                earlier = f"{earlier}\n\n{current['text'].strip()}".strip()
                current = {
                    **more[-1],
                    "text": "\n\n".join(p["text"].strip() for p in more),
                    # files sent with the dropped reply's messages are still part of what was said
                    "attachments": [*(current.get("attachments") or []), *(a for p in more for a in p.get("attachments") or [])],
                }
            finally:
                with self._cond:
                    self._current = None

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._inbox and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                payload = self._inbox.pop(0)
            try:
                self._answer(payload)
            except Exception:
                logger.exception("handling a message failed")
                try:
                    self._service.send_message(payload.get("audience"), "⚠️ 處理這則訊息時發生未預期錯誤，請查看日誌。")
                except Exception:
                    logger.exception("reporting the failure failed")
            self._bus.publish()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()


def _json_body(handler: BaseHTTPRequestHandler, limit: int = MAX_BODY_BYTES) -> dict | None:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError:
        return None
    if length > limit:
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

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        scheme, _, supplied = header.partition(" ")
        return supplied.strip() if scheme.lower() == "bearer" else ""

    def _authorized(self, path: str = "") -> bool:
        """Two tokens, two doors: the main token opens everything except /v1/peer/*, the peer token opens
        only /v1/peer/*. Neither opens the other's routes, so a leaked peer token cannot approve an action
        and a service holding the main token cannot pose as a peer."""
        supplied = self._bearer()
        if not supplied:
            return False
        is_peer_route = path.startswith("/v1/peer/") or path == "/v1/peer"
        if is_peer_route:
            token = self.api.peer_token
        else:
            token = self.api.token
        return bool(token) and hmac.compare_digest(supplied, token)

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
        if not self._authorized(path):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            if path.startswith("/v1/peer/requests"):
                self._peer_get(path, query)
            elif path == "/v1/peer-links":
                # The main token's view, for the status page: names, trust, addresses; never a token or a hash.
                from memtrace_harness.peer_links import PeerLinks, links_path

                self._send(200, PeerLinks(links_path(self.api.config)).public_view())
            elif path == "/v1/peer/links":
                from memtrace_harness.peer_links import PeerLinks, links_path

                self._send(200, PeerLinks(links_path(self.api.config)).snapshot_for_service())
            elif path == "/v1/peer/profile":
                from memtrace_harness.peer_links import harness_name

                self._send(200, {
                    "name": harness_name(self.api.config, self.api.scope),
                    "description": self.api.config.peer_description_now(),
                })
            elif path == "/v1/peer/outbound":
                outbound = self.api.outbound
                if outbound is None:
                    self._send(404, {"error": "not found"})
                else:
                    wait = self._float(query, "wait", 0, MAX_WAIT_SECONDS)
                    self._send(200, {"requests": outbound.claim(wait)})
            elif path == "/v1/commands":
                self._send(200, {"commands": self.api.service.commands()})
            elif path == "/v1/observations":
                from memtrace_harness.improvement import export_observations
                self._send(200, export_observations(self.api.service.approval_manager.trace_store, self.api.config, self.api.scope, int(query.get("after", "0"))))
            elif path == "/v1/improvement":
                from memtrace_harness.improvement import ImprovementStore
                self._send(200, ImprovementStore(self.api.service.approval_manager.trace_store).public(self.api.config, self.api.scope))
            elif path == "/v1/events":
                limit = int(self._float(query, "limit", 20, 100))
                wait = self._float(query, "wait", 0, MAX_WAIT_SECONDS)
                consumer = query.get("consumer")
                self._send(
                    200,
                    {"events": self.api.service.fetch_events(
                        limit=limit, wait_seconds=wait, consumer=consumer if consumer in {"web"} else None
                    )},
                )
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
        body = _json_body(self, MAX_MESSAGE_BODY_BYTES if path == "/v1/messages" else MAX_BODY_BYTES)
        if not self._authorized(path):
            self._send(401, {"error": "unauthorized"})
            return
        if body is None:
            self._send(400, {"error": "malformed request body"})
            return
        try:
            if path.startswith("/v1/peer/requests"):
                self._peer_post(path, body)
            elif path.startswith("/v1/peer/outbound/") or path in ("/v1/peer/directory", "/v1/peer/endpoint"):
                self._outbound_post(path, body)
            elif path == "/v1/messages":
                text = body.get("text")
                try:
                    uploads = attachments.decode_uploads(body.get("attachments"))
                except attachments.AttachmentError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                if not isinstance(text, str) or not (text.strip() or uploads):
                    self._send(400, {"error": "text is required"})
                    return
                # Saved here, so the bytes do not wait in memory behind a slow reply.
                saved = [self.api.service.attachments.save(u["name"], u["mime"], u["data"]) for u in uploads]
                self.api.worker.submit(
                    {
                        "text": text.strip() or "（傳了附件）",
                        "audience": _opt_str(body.get("audience")),
                        "reply_to_ref": _opt_str(body.get("reply_to_ref")),
                        "reply_to_text": _opt_str(body.get("reply_to_text")),
                        "attachments": saved,
                    }
                )
                self._send(202, {"accepted": True})
            elif path == "/v1/observations":
                from memtrace_harness.improvement import ImprovementStore
                count = ImprovementStore(self.api.service.approval_manager.trace_store).ingest(self.api.config, body)
                self.api.bus.publish()
                self._send(200, {"accepted": count})
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

    # ---- another Harness's questions (peer.py); the peer token is the only one that reaches these
    def _peer(self, source: dict) -> str | None:
        peer = source.get("peer")
        return peer.strip() if isinstance(peer, str) and peer.strip() and len(peer) <= 64 else None

    def _peer_get(self, path: str, query: dict[str, str]) -> None:
        peers = self.api.peers
        peer = self._peer(query)
        if peers is None or peer is None:
            self._send(400, {"error": "peer is required"})
            return
        parts = path.split("/")  # ["", "v1", "peer", "requests", <id>?]
        if len(parts) == 4:
            self._send(200, {"requests": peers.list(peer, query.get("context_id") or None)})
        elif len(parts) == 5:
            found = peers.get(peer, parts[4])
            self._send(200 if found else 404, {"request": found} if found else {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def _peer_post(self, path: str, body: dict) -> None:
        peers = self.api.peers
        peer = self._peer(body)
        if peers is None or peer is None:
            self._send(400, {"error": "peer is required"})
            return
        parts = path.split("/")
        if len(parts) == 4:
            hops = body.get("hops", 0)
            text = body.get("text")
            if not isinstance(text, str) or isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
                self._send(400, {"error": "text and a non-negative hops are required"})
                return
            created = peers.submit(
                peer=peer, context_id=str(body.get("context_id") or "")[:200] or f"ctx_{os.urandom(6).hex()}",
                text=text, skill=str(body.get("skill") or "ask"), hops=hops,
            )
            self._send(202, {"request": created})
        elif len(parts) == 6 and parts[5] == "cancel":
            found = peers.cancel(peer, parts[4])
            self._send(200 if found else 404, {"request": found} if found else {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def _outbound_post(self, path: str, body: dict) -> None:
        outbound = self.api.outbound
        if outbound is None:
            self._send(404, {"error": "not found"})
            return
        if path == "/v1/peer/endpoint":
            from memtrace_harness.peer_links import LinkError, PeerLinks, links_path

            try:
                PeerLinks(links_path(self.api.config)).set_endpoint(str(body.get("url") or ""))
            except LinkError as exc:
                self._send(400, {"error": str(exc)})
                return
            self.api.bus.publish()
            self._send(200, {"ok": True})
            return
        if path == "/v1/peer/directory":
            peers = body.get("peers")
            if not isinstance(peers, list):
                self._send(400, {"error": "peers is required"})
                return
            self._send(200, {"count": outbound.set_directory(peers)})
            return
        parts = path.split("/")  # ["", "v1", "peer", "outbound", <id>, "result"]
        if len(parts) != 6 or parts[5] != "result":
            self._send(404, {"error": "not found"})
            return
        state, text, reason = body.get("state"), body.get("text"), body.get("reason")
        if state not in ("completed", "failed") or any(v is not None and not isinstance(v, str) for v in (text, reason)):
            self._send(400, {"error": "state (completed or failed) is required"})
            return
        self._send(200, {"accepted": outbound.complete(parts[4], state, text=text, reason=reason)})

    def _admin(self, setting: str, body: dict) -> None:
        config = self.api.config
        try:
            if setting == "execution-source":
                entry = admin.execution_source_action(config, body)
                self.api.bus.publish()
                self._send(200, {"ok": True, "entry": entry})
                return
            elif setting == "execution-source-allowlist":
                admin.update_execution_source_allowlist(config, body.get("allowed"))
            elif setting == "role-profile":
                admin.update_role_profile(config, str(body["profile_id"]), str(body["provider"]), str(body["model"]))
            elif setting == "role-profile-chain":
                admin.update_role_profile_chain(config, str(body["profile_id"]), body["chain"])
            elif setting in ("chat-chain", "digest-chain", "archive-chain", "audit-chain", "recall-chain"):
                admin.update_model_chain(config, setting.split("-")[0], body["chain"])
            elif setting == "model-catalog-refresh":
                admin.refresh_model_catalog()
            elif setting == "role-profile-independent":
                dest = admin.create_dedicated_role_profiles_file(config)
                self.api.bus.publish()
                self._send(200, {"ok": True, "path": str(dest)})
                return
            elif setting.startswith("peer-link-"):
                result = admin.peer_link_action(config, setting[len("peer-link-"):], body)
                self.api.bus.publish()
                self._send(200, {"ok": True, **result})
                return
            elif setting == "peer-name":
                admin.update_peer_name(config, body["name"])
            elif setting == "peer-description":
                admin.update_peer_description(config, body["description"])
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
            elif setting == "memory-feature-enabled":
                admin.update_automation_enabled(config, str(body.get("feature", "")), body.get("enabled"))
            elif setting == "improvement":
                from memtrace_harness.improvement import update_settings
                update_settings(config, body)
            elif setting == "approval":
                self._resolve_approval(body)
                return
            elif setting == "preference":
                self._resolve_preference(body)
                return
            elif setting == "shared-memory":
                from memtrace_harness.shared_memory import SharedMemoryStore
                if config.shared_memory_path is None:
                    raise ValueError("shared memory is not configured")
                entry = SharedMemoryStore(config.shared_memory_path).change(body)
                self.api.bus.publish()
                self._send(200, {"ok": True, "entry": entry})
                return
            elif setting == "stuck":
                self._resolve_stuck(body)
                return
            elif setting == "chat-question":
                self._resolve_chat_question(body)
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

    def _resolve_chat_question(self, body: dict) -> None:
        question_id, choice = body.get("question_id"), body.get("choice")
        dismiss = body.get("action") == "dismiss"
        if not isinstance(question_id, str) or not (
            dismiss or (isinstance(choice, int) and not isinstance(choice, bool))
        ):
            self._send(400, {"error": "malformed request body"})
            return
        service = self.api.service
        if service.approval_manager.trace_store.get_chat_question(question_id) is None:
            self._send(400, {"error": "找不到這個問題"})
            return
        msg = service.resolve_chat_question(question_id, None if dismiss else choice)
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
        abandoned, stopped = self.api.service.abandon_task(self.api.scope.workspace_id, conversation_id)
        if not abandoned:
            self._send(400, {"error": "找不到這個未完成的任務"})
            return
        self.api.bus.publish()
        extra = f"，並終止了它還在跑的 {stopped} 個 CLI 程序" if stopped else ""
        self._send(200, {"ok": True, "message": f"已放棄 {conversation_id}{extra}，工作區已釋放。"})

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
        self.peer_token = config.peer_api_token
        self.bus = bus or ChangeBus()
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.worker = MessageWorker(service, self.bus)
        self.peers = (
            PeerAskService(config, scope, service.approval_manager.trace_store, on_change=self.bus.publish)
            if self.peer_token
            else None
        )
        if self.peers is not None:
            service.on_task_finished = self.peers.task_finished
        self.outbound = (
            PeerOutbound(service.approval_manager.trace_store, self._deliver_peer_answer, self.bus.publish)
            if self.peer_token
            else None
        )
        self._server: ThreadingHTTPServer | None = None

    def _deliver_peer_answer(self, project: str, text: str) -> None:
        """A peer's reply (or the lack of one): to the operator, and into the transcript so the chat model knows."""
        self.service.send_message(None, text)
        self.service._record_order_turn(project, text)

    def status(self) -> dict:
        return admin.collect_status_data(self.config, self.scope, self.started_at)

    def start(self) -> ThreadingHTTPServer:
        if not self.token:
            raise RuntimeError(
                "HARNESS_API_TOKEN is not set in .harness.env: the API refuses to start without a token"
            )
        if self.peer_token and self.peer_token == self.token:
            raise RuntimeError("HARNESS_PEER_API_TOKEN must differ from HARNESS_API_TOKEN")
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
