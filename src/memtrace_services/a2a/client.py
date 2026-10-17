"""Asking another Harness over A2A 1.0 (JSON-RPC): read its Agent Card, SendMessage, follow the Task.

Calls only the peers the operator listed under ``[outbound.*]``. Redirects are never followed (a peer cannot
bounce this service to another host), responses are size-capped, and what comes back is returned as text for
the Harness to show as unverified data."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

from memtrace_services.a2a.config import OutboundPeer
from memtrace_services.a2a.protocol import HOPS_KEY, PROTOCOL_VERSION, SKILL_KEY, TERMINAL

MAX_RESPONSE_BYTES = 200_000
STATE_NAMES = {f"TASK_STATE_{s.upper()}": s for s in ("submitted", "working", "completed", "failed", "canceled", "rejected")}
STATE_NAMES |= {"TASK_STATE_INPUT_REQUIRED": "input_required", "TASK_STATE_AUTH_REQUIRED": "auth_required"}


class PeerCallError(RuntimeError):
    """The peer could not be asked; the text is safe to show the operator."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class PeerClient:
    def __init__(
        self,
        peer: OutboundPeer,
        *,
        timeout: float = 30.0,
        deadline_seconds: float = 300.0,
        poll_seconds: float = 2.0,
        collaborate_deadline_seconds: float = 7 * 3600.0,
        collaborate_poll_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.peer = peer
        self.timeout, self.deadline, self.poll_seconds, self._sleep = timeout, deadline_seconds, poll_seconds, sleep
        self.collaborate_deadline, self.collaborate_poll = collaborate_deadline_seconds, collaborate_poll_seconds
        self._rpc_url: str | None = None

    # ---- plumbing
    def _open(self, request: urllib.request.Request) -> bytes:
        try:
            with _OPENER.open(request, timeout=self.timeout) as resp:
                return resp.read(MAX_RESPONSE_BYTES + 1)[:MAX_RESPONSE_BYTES]
        except urllib.error.HTTPError as exc:
            retry_after = None
            if exc.code == 429:
                try:
                    retry_after = max(1.0, min(60.0, float((exc.headers or {}).get("Retry-After", "60"))))
                except (TypeError, ValueError):
                    retry_after = 60.0
            elif exc.code in (502, 503, 504):
                retry_after = 5.0
            exc.close()
            raise PeerCallError(f"peer answered HTTP {exc.code}", retry_after=retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise PeerCallError("peer unreachable", retry_after=5.0) from None

    def card_description(self) -> str | None:
        """What the peer says about itself in its (public) Agent Card, or None when it cannot be read. Single line, capped."""
        try:
            card = json.loads(self._open(urllib.request.Request(f"{self.peer.url}/.well-known/agent-card.json")).decode("utf-8"))
        except (PeerCallError, ValueError):
            return None
        text = card.get("description") if isinstance(card, dict) else None
        return " ".join(text.split())[:600] if isinstance(text, str) and text.strip() else None

    def _card_rpc_url(self) -> str:
        if self._rpc_url is None:
            raw = self._open(urllib.request.Request(f"{self.peer.url}/.well-known/agent-card.json"))
            try:
                card = json.loads(raw.decode("utf-8"))
                interfaces = card["supportedInterfaces"]
            except (ValueError, KeyError, TypeError):
                raise PeerCallError("peer's Agent Card is unreadable") from None
            for item in interfaces if isinstance(interfaces, list) else []:
                if isinstance(item, dict) and item.get("protocolBinding") == "JSONRPC" \
                        and item.get("protocolVersion") == PROTOCOL_VERSION and isinstance(item.get("url"), str):
                    self._rpc_url = item["url"]
                    break
            else:
                raise PeerCallError(f"peer does not offer JSON-RPC {PROTOCOL_VERSION}")
        return self._rpc_url

    def _rpc(self, method: str, params: dict) -> dict:
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex[:8], "method": method, "params": params}
        request = urllib.request.Request(
            self._card_rpc_url(), data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json", "A2A-Version": PROTOCOL_VERSION,
                "Authorization": f"Bearer {self.peer.token}",
            },
        )
        try:
            reply = json.loads(self._open(request).decode("utf-8"))
        except ValueError:
            raise PeerCallError("peer's reply is not JSON") from None
        if not isinstance(reply, dict):
            raise PeerCallError("peer's reply is malformed")
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            raise PeerCallError(f"peer refused: {str(error.get('message') or 'error')[:120]}")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise PeerCallError("peer's reply is malformed")
        return result

    # ---- the one thing we do
    def ask(self, question: str, context_id: str, hops: int, skill: str = "ask") -> tuple[str, str | None, str | None]:
        """(state, answer text, reason): state is completed or failed. A ``collaborate`` request may wait hours (the
        peer diagnoses, its operator approves, a fix runs), so it keeps polling through ``input_required``."""
        collaborate = skill == "collaborate"
        deadline = self.collaborate_deadline if collaborate else self.deadline
        poll = self.collaborate_poll if collaborate else self.poll_seconds
        message = {
            "messageId": uuid.uuid4().hex, "contextId": context_id, "role": "ROLE_USER",
            "parts": [{"text": question}], "metadata": {HOPS_KEY: hops, SKILL_KEY: skill},
        }
        # Without returnImmediately the peer holds SendMessage open for up to BLOCK_SECONDS (50), longer than this
        # client's per-call timeout, so a request that took over `timeout` seconds was reported "peer unreachable"
        # while the peer was still working on it. Ask for the task at once and poll GetTask instead.
        task = self._rpc("SendMessage", {"message": message, "configuration": {"returnImmediately": True}}).get("task")
        if not isinstance(task, dict):
            raise PeerCallError("peer did not return a task")
        expires = time.monotonic() + deadline
        delay = poll
        while self._state(task) not in TERMINAL:
            if self._state(task) == "auth_required" or (self._state(task) == "input_required" and not collaborate):
                return "failed", None, "the peer wants more input, which this Harness does not give"
            remaining = expires - time.monotonic()
            if remaining <= 0:
                try:
                    self._rpc("CancelTask", {"id": task.get("id")})
                except PeerCallError:
                    pass
                return "failed", None, "the peer did not answer in time"
            self._sleep(min(delay, remaining))
            if time.monotonic() >= expires:
                continue
            try:
                task = self._rpc("GetTask", {"id": task.get("id")})
                delay = poll
            except PeerCallError as exc:
                if exc.retry_after is None:
                    raise
                # Only repeat a read of the known task. Replaying SendMessage could start duplicate work.
                delay = max(poll, exc.retry_after)
        return self._outcome(task)

    @staticmethod
    def _state(task: dict) -> str:
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        return STATE_NAMES.get(str(status.get("state")), "unknown")

    def _outcome(self, task: dict) -> tuple[str, str | None, str | None]:
        state = self._state(task)
        if state == "completed":
            texts = [
                part["text"]
                for artifact in task.get("artifacts") or [] if isinstance(artifact, dict)
                for part in artifact.get("parts") or [] if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            return ("completed", "\n".join(texts), None) if texts else ("failed", None, "the peer's answer was empty")
        message = (task.get("status") or {}).get("message")
        reason = next(
            (p["text"] for p in (message or {}).get("parts", []) if isinstance(p, dict) and isinstance(p.get("text"), str)),
            None,
        ) if isinstance(message, dict) else None
        return "failed", None, f"the peer's task ended {state}" + (f": {reason[:120]}" if reason else "")
