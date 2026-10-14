"""The Harness's /v1/peer/* routes, from the A2A service's side."""

from __future__ import annotations

import urllib.parse

from memtrace_services.client import HarnessClient, HarnessError


class PeerGateway(HarnessClient):
    def submit(self, *, peer: str, context_id: str, text: str, skill: str, hops: int) -> dict:
        body = {"peer": peer, "context_id": context_id, "text": text, "skill": skill, "hops": hops}
        return self._request("POST", "/v1/peer/requests", body)["request"]

    def get(self, peer: str, request_id: str) -> dict | None:
        query = urllib.parse.urlencode({"peer": peer})
        try:
            return self._request("GET", f"/v1/peer/requests/{urllib.parse.quote(request_id, safe='')}?{query}")["request"]
        except HarnessError as exc:
            if exc.status == 404:
                return None
            raise

    def list(self, peer: str, context_id: str | None = None) -> list[dict]:
        params = {"peer": peer, **({"context_id": context_id} if context_id else {})}
        return self._request("GET", f"/v1/peer/requests?{urllib.parse.urlencode(params)}")["requests"]

    def cancel(self, peer: str, request_id: str) -> dict | None:
        try:
            return self._request(
                "POST", f"/v1/peer/requests/{urllib.parse.quote(request_id, safe='')}/cancel", {"peer": peer}
            )["request"]
        except HarnessError as exc:
            if exc.status == 404:
                return None
            raise

    # ---- the other direction: questions this Harness asks (peer.py PeerOutbound)
    def claim_outbound(self, wait: float = 0.0) -> list[dict]:
        query = urllib.parse.urlencode({"wait": wait})
        return self._request("GET", f"/v1/peer/outbound?{query}", timeout=wait + self.timeout)["requests"]

    def report_outbound(self, request_id: str, state: str, *, text: str | None = None, reason: str | None = None) -> bool:
        body = {"state": state, "text": text, "reason": reason}
        return bool(self._request(
            "POST", f"/v1/peer/outbound/{urllib.parse.quote(request_id, safe='')}/result", body
        ).get("accepted"))

    def sync_directory(self, peers: list[dict]) -> None:
        self._request("POST", "/v1/peer/directory", {"peers": peers})

    def profile(self) -> dict:
        """{name, description}: what the operator says this Harness is, set on the status page."""
        return self._request("GET", "/v1/peer/profile")

    def links(self) -> dict:
        """{inbound: [{name, token_sha256, trust}], outbound: [{name, url, token, collaborate}]}: the links the operator made
        on the status page (the Harness keeps them; secrets included, so only this peer-token route gives them out)."""
        return self._request("GET", "/v1/peer/links")

    def register_endpoint(self, url: str) -> None:
        self._request("POST", "/v1/peer/endpoint", {"url": url})
