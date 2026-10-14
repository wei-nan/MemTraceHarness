"""A2A 1.0 (JSON-RPC binding), the part this service speaks: Agent Card, SendMessage, GetTask,
ListTasks, CancelTask. Streaming, push notifications and the extended card are declared unsupported.

Pure functions plus a dispatcher over a gateway; no sockets here (server.py has those)."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from memtrace_services.a2a.config import A2aConfig, PeerEntry
from memtrace_services.client import HarnessError, HarnessUnavailable

PROTOCOL_VERSION = "1.0"
HOPS_KEY = "memtrace.harness/hops"
SKILL_KEY = "memtrace.harness/skill"
BLOCK_SECONDS = 50.0
POLL_SECONDS = 0.4

STATES = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input_required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "rejected": "TASK_STATE_REJECTED",
}
TERMINAL = ("completed", "failed", "canceled", "rejected")

# JSON-RPC and A2A error codes (spec, JSON-RPC binding).
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
TASK_NOT_FOUND, TASK_NOT_CANCELABLE, PUSH_NOT_SUPPORTED, UNSUPPORTED_OPERATION = -32001, -32002, -32003, -32004
CONTENT_TYPE_NOT_SUPPORTED, VERSION_NOT_SUPPORTED = -32005, -32009
TOO_MANY_TASKS = -32010  # ours: this peer already has its allowed number of unfinished questions


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def agent_card(config: A2aConfig, description: str | None = None, name: str | None = None) -> dict:
    """What anyone may read: no paths, no limits, no other peers' names. `description` is what the Harness's operator wrote
    on the status page and `name` is what the Harness is called there; the file's own are the fallback when the Harness
    has none or cannot be reached."""
    return {
        "name": (name or "").strip() or config.name,
        "description": (description or "").strip() or config.description,
        "version": "1",
        "supportedInterfaces": [
            {"url": f"{config.public_url}/rpc", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "securitySchemes": {"peerToken": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"peerToken": {"list": []}}}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "ask",
                "name": "Ask",
                "description": "Answer a question from this project's knowledge base. Read-only: it never starts work.",
                "tags": ["knowledge", "read-only"],
                "examples": ["What was decided about the data pipeline last week?"],
            },
            {
                "id": "collaborate",
                "name": "Collaborate",
                "description": (
                    "For peers this Harness's operator trusts: report a problem for it to investigate and, if the "
                    "operator approves, fix. The task stays in input-required until that approval."
                ),
                "tags": ["diagnose", "fix"],
            },
        ],
    }


def render_task(request: dict) -> dict:
    """A Harness peer request as an A2A Task. The only words a peer ever sees beyond the answer are the
    fixed reasons the Harness itself chose."""
    state = request["state"]
    status: dict[str, Any] = {"state": STATES.get(state, "TASK_STATE_UNSPECIFIED"), "timestamp": request["updated_at"]}
    if state in ("failed", "rejected", "input_required") and request.get("reason"):
        status["message"] = {
            "messageId": uuid.uuid4().hex, "taskId": request["request_id"], "contextId": request["context_id"],
            "role": "ROLE_AGENT", "parts": [{"text": request["reason"]}],
        }
    task: dict[str, Any] = {"id": request["request_id"], "contextId": request["context_id"], "status": status}
    if state == "completed" and request.get("result"):
        task["artifacts"] = [{"artifactId": "answer", "name": "answer", "parts": [{"text": request["result"]}]}]
    return task


def _message_text(message: Any) -> str:
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not message["parts"]:
        raise RpcError(INVALID_PARAMS, "message.parts is required")
    texts = []
    for part in message["parts"]:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            raise RpcError(CONTENT_TYPE_NOT_SUPPORTED, "only text parts are supported")
        texts.append(part["text"])
    return "\n".join(texts)


class Dispatcher:
    def __init__(
        self,
        config: A2aConfig,
        gateway: Any,
        *,
        block_seconds: float = BLOCK_SECONDS,
        poll_seconds: float = POLL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config, self.gateway = config, gateway
        self.block_seconds, self.poll_seconds, self._sleep = block_seconds, poll_seconds, sleep

    # ---- one JSON-RPC request in, one response object out
    def handle(self, peer: PeerEntry, request: Any) -> dict:
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise RpcError(INVALID_REQUEST, "not a JSON-RPC 2.0 request")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise RpcError(INVALID_PARAMS, "params must be an object")
            result = self._call(peer, request["method"], params)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as exc:
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": exc.message}}
        except (HarnessUnavailable, HarnessError):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": INTERNAL_ERROR, "message": "temporarily unavailable"}}

    def _call(self, peer: PeerEntry, method: str, params: dict) -> dict:
        if method == "SendMessage":
            return self._send(peer, params)
        if method == "GetTask":
            return self._get(peer, params)
        if method == "ListTasks":
            return self._list(peer, params)
        if method == "CancelTask":
            return self._cancel(peer, params)
        if method in ("SendStreamingMessage", "SubscribeToTask", "GetExtendedAgentCard"):
            raise RpcError(UNSUPPORTED_OPERATION, f"{method} is not supported")
        if "PushNotificationConfig" in method:
            raise RpcError(PUSH_NOT_SUPPORTED, "push notifications are not supported")
        raise RpcError(METHOD_NOT_FOUND, f"unknown method {method}")

    # ---- methods
    def _send(self, peer: PeerEntry, params: dict) -> dict:
        message = params.get("message")
        text = _message_text(message)
        if message.get("taskId"):
            # An input-required task waits for its operator's approval, not for another message, so nothing continues.
            if self.gateway.get(peer.name, str(message["taskId"])) is None:
                raise RpcError(TASK_NOT_FOUND, "task not found")
            raise RpcError(UNSUPPORTED_OPERATION, "this task is not waiting for input")
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        skill = str(metadata.get(SKILL_KEY) or "ask")
        if skill not in peer.skills:
            raise RpcError(UNSUPPORTED_OPERATION, "skill not allowed")
        hops = metadata.get(HOPS_KEY, 0)
        if isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
            raise RpcError(INVALID_PARAMS, f"{HOPS_KEY} must be a non-negative integer")
        context_id = str(message.get("contextId") or "")[:200] or f"ctx_{uuid.uuid4().hex[:12]}"
        unfinished = sum(1 for r in self.gateway.list(peer.name) if r["state"] not in TERMINAL)
        if unfinished >= peer.max_concurrent:
            raise RpcError(TOO_MANY_TASKS, "too many unfinished tasks")
        request = self.gateway.submit(peer=peer.name, context_id=context_id, text=text, skill=skill, hops=hops)
        configuration = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        if not configuration.get("returnImmediately"):
            request = self._wait(peer.name, request)
        return {"task": render_task(request)}

    def _wait(self, peer: str, request: dict) -> dict:
        """Blocking send: wait for the answer, but only for a while; after that the client polls GetTask."""
        deadline = time.monotonic() + self.block_seconds
        while request["state"] not in TERMINAL + ("input_required",) and time.monotonic() < deadline:
            self._sleep(self.poll_seconds)
            request = self.gateway.get(peer, request["request_id"]) or request
        return request

    def _get(self, peer: PeerEntry, params: dict) -> dict:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, "id is required")
        request = self.gateway.get(peer.name, task_id)
        if request is None:
            raise RpcError(TASK_NOT_FOUND, "task not found")
        return render_task(request)

    def _list(self, peer: PeerEntry, params: dict) -> dict:
        context = params.get("contextId")
        tasks = [render_task(r) for r in self.gateway.list(peer.name, context if isinstance(context, str) else None)]
        return {"tasks": tasks, "nextPageToken": "", "pageSize": len(tasks), "totalSize": len(tasks)}

    def _cancel(self, peer: PeerEntry, params: dict) -> dict:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise RpcError(INVALID_PARAMS, "id is required")
        before = self.gateway.get(peer.name, task_id)
        if before is None:
            raise RpcError(TASK_NOT_FOUND, "task not found")
        if before["state"] in TERMINAL:
            raise RpcError(TASK_NOT_CANCELABLE, "task is already finished")
        request = self.gateway.cancel(peer.name, task_id)
        if request is None:
            raise RpcError(TASK_NOT_FOUND, "task not found")
        return render_task(request)
