"""A Harness's local HTTP API, from a service's side (see memtrace_harness/api.py for the routes)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HarnessUnavailable(RuntimeError):
    """The Harness did not answer (not running, restarting, refused the connection)."""


class HarnessError(RuntimeError):
    """The Harness answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class HarnessClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8")).get("error", exc.reason)
            except Exception:
                message = str(exc.reason)
            raise HarnessError(exc.code, str(message)) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HarnessUnavailable(str(exc)) from None
        return json.loads(raw) if raw else {}

    # ---- conversation
    def post_message(
        self,
        text: str,
        *,
        audience: str | None,
        reply_to_ref: str | None = None,
        reply_to_text: str | None = None,
    ) -> None:
        self._request(
            "POST",
            "/v1/messages",
            {"text": text, "audience": audience, "reply_to_ref": reply_to_ref, "reply_to_text": reply_to_text},
        )

    def post_action(self, action_id: str, *, audience: str | None, message_ref: str | None = None) -> str | None:
        return self._request(
            "POST", "/v1/actions", {"id": action_id, "audience": audience, "message_ref": message_ref}
        ).get("toast")

    def commands(self) -> list[dict[str, str]]:
        return self._request("GET", "/v1/commands").get("commands", [])

    # ---- outbox
    def events(self, *, limit: int = 20, wait: float = 0.0) -> list[dict]:
        query = urllib.parse.urlencode({"limit": limit, "wait": wait})
        return self._request("GET", f"/v1/events?{query}", timeout=wait + self.timeout).get("events", [])

    def ack(self, event_id: int, *, ok: bool, message_refs: list[str] | None = None) -> str:
        return self._request(
            "POST", f"/v1/events/{event_id}/ack", {"ok": ok, "message_refs": message_refs or []}
        ).get("result", "")

    # ---- status and admin
    def health(self) -> dict:
        return self._request("GET", "/v1/health", timeout=3.0)

    def status(self) -> dict:
        return self._request("GET", "/v1/status", timeout=30.0)

    def changes(self, since: int, *, wait: float = 0.0) -> int:
        query = urllib.parse.urlencode({"since": since, "wait": wait})
        return int(self._request("GET", f"/v1/changes?{query}", timeout=wait + self.timeout).get("version", 0))

    def turn(self, turn_id: int) -> dict | None:
        try:
            return self._request("GET", f"/v1/turns/{int(turn_id)}")
        except HarnessError as exc:
            if exc.status == 404:
                return None
            raise

    def admin(self, setting: str, body: dict) -> dict:
        return self._request("POST", f"/v1/admin/{setting}", body, timeout=60.0)
