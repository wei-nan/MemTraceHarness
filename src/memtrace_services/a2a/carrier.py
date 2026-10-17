"""Carries this Harness's questions to other Harnesses and brings the answers back.

Long-polls the Harness for queued questions (``/v1/peer/outbound``), asks the named peer over A2A, and reports the
outcome. Also tells the Harness which peers it can reach, so the model's ``ask_peer`` knows their names."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from memtrace_services.a2a.client import PeerCallError, PeerClient
from memtrace_services.a2a.config import A2aConfig, LiveConfig
from memtrace_services.client import HarnessError, HarnessUnavailable

logger = logging.getLogger(__name__)

DIRECTORY_REFRESH_SECONDS = 300.0
POLL_WAIT_SECONDS = 20.0


class Carrier:
    def __init__(
        self,
        config: A2aConfig,
        gateway,
        *,
        client_factory: Callable[[object], PeerClient] = PeerClient,
    ) -> None:
        self.config, self.gateway = config, gateway
        self._client_factory = client_factory
        self._clients: dict[str, tuple[object, PeerClient]] = {}
        self._last_sync = 0.0
        self._synced_for: frozenset | None = None

    def _client(self, name: str) -> PeerClient | None:
        """The client for a peer as it is configured now (made again if its address or token changed)."""
        current = self.config.current(force=True) if isinstance(self.config, LiveConfig) else self.config
        peer = current.outbound.get(name)
        if peer is None:
            self._clients.pop(name, None)
            return None
        cached = self._clients.get(name)
        if cached is None or cached[0] != peer:
            cached = (peer, self._client_factory(peer))
            self._clients[name] = cached
        return cached[1]

    def _outbound_signature(self) -> frozenset:
        return frozenset((p.name, p.url, p.collaborate, p.display_name) for p in self.config.outbound.values())

    def sync_directory(self) -> None:
        """Tell the Harness who it can reach, each described in the peer's own words (its Agent Card); the description in
        this file is only the fallback for a peer that cannot be read right now."""
        peers = []
        for p in list(self.config.outbound.values()):
            own = None
            try:
                own = self._client(p.name).card_description()
            except Exception:  # noqa: BLE001 — a peer being down must not stop the directory from syncing
                logger.warning(f"could not read {p.name}'s Agent Card")
            description = own or p.description
            if p.display_name and p.display_name != p.name:
                description = f"{p.display_name}: {description}"
            peers.append({"name": p.name, "description": description, "collaborate": p.collaborate})
        self.gateway.sync_directory(peers)
        self._last_sync = time.monotonic()
        self._synced_for = self._outbound_signature()
        try:  # the status page needs to know where to point the other side of a pairing
            self.gateway.register_endpoint(self.config.public_url)
        except (HarnessUnavailable, HarnessError):
            logger.debug("could not tell the Harness this service's address")

    def handle(self, item: dict) -> None:
        """One question: ask, then report whatever happened. Always reports, so the operator is never left waiting."""
        request_id = item["request_id"]
        client = self._client(item["peer"])
        try:
            if client is None:
                state, text, reason = "failed", None, "this Harness has no such peer configured"
            else:
                state, text, reason = client.ask(
                    item["question"], item["context_id"], int(item.get("hops", 0)), str(item.get("skill") or "ask")
                )
        except PeerCallError as exc:
            state, text, reason = "failed", None, str(exc)
        except Exception:  # noqa: BLE001 — the operator must still hear about it
            logger.exception(f"asking {item.get('peer')} failed")
            state, text, reason = "failed", None, "carrier error"
        try:
            self.gateway.report_outbound(request_id, state, text=text, reason=reason)
        except (HarnessUnavailable, HarnessError):
            logger.exception(f"could not report the outcome of {request_id}")

    def run_once(self, wait: float = 0.0) -> int:
        """Claim what is queued and carry each question on its own thread. How many were claimed."""
        if (
            not self._last_sync
            or time.monotonic() - self._last_sync >= DIRECTORY_REFRESH_SECONDS
            or self._outbound_signature() != self._synced_for  # a link was made or removed on the status page
        ):
            self.sync_directory()
        items = self.gateway.claim_outbound(wait)
        for item in items:
            threading.Thread(target=self.handle, args=(item,), daemon=True, name=f"ask-{item['request_id']}").start()
        return len(items)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once(POLL_WAIT_SECONDS)
            except (HarnessUnavailable, HarnessError):
                logger.warning("the Harness is not answering; retrying")
                stop.wait(5.0)
