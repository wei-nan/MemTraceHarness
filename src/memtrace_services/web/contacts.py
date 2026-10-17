"""Trusted directory and contact-request relay for the Harnesses configured by the operator.

Only local Harness HTTP APIs are used. No credential is sent to an address supplied by a model,
and no connection secret crosses this process. A public cross-machine handshake is future work.
"""
from __future__ import annotations

import logging
import threading

from memtrace_services.client import HarnessError, HarnessUnavailable

logger = logging.getLogger(__name__)


class ContactRelay:
    def __init__(self, targets):
        self.targets = targets
        self._stop = threading.Event()

    def run_once(self):
        registry = {}
        for target in self.targets:
            try:
                view = target.client()._request("GET", "/v1/contacts/relay")
                if view.get("identity_id") and view.get("endpoint"):
                    if view["identity_id"] in registry:
                        raise ValueError("duplicate Harness identity in contact relay")
                    registry[view["identity_id"]] = (target, view)
            except (HarnessError, HarnessUnavailable):
                continue
        entries = [{"id": identity, "name": view["name"], "url": view["endpoint"],
                    "description": view.get("description", "")} for identity, (_, view) in registry.items()]
        ready = set()
        for identity, (target, _) in registry.items():
            try:
                target.client().admin("contact-directory", {"peers": entries})
                ready.add(identity)
            except (HarnessError, HarnessUnavailable):
                continue
        for identity, (source, view) in registry.items():
            if identity not in ready:
                continue
            for outgoing in view.get("outgoing", []):
                destination = registry.get(outgoing["target_id"])
                if not destination or destination[1]["identity_id"] not in ready:
                    continue
                if outgoing["state"] not in ("pending", "approved", "revoked", "expired"):
                    continue
                # Bind identity and label to the authenticated source API, not submitted fields.
                request = {**outgoing, "source_id": identity, "source_name": view["name"],
                           "target_name": destination[1]["name"]}
                try:
                    if outgoing["state"] in ("revoked", "expired"):
                        destination[0].client().admin("contact-withdraw", request)
                        if outgoing["state"] == "expired":
                            source.client().admin("contact-result", outgoing)
                        continue
                    result = destination[0].client().admin("contact-incoming", request)["request"]
                    # The public response intentionally omits the hash; the source checks its own binding.
                    source.client().admin("contact-result", {**result, "token_sha256": outgoing["token_sha256"]})
                except (HarnessError, HarnessUnavailable):
                    logger.warning("contact relay will retry an unsettled request")

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("contact relay pass failed")
            self._stop.wait(2)

    def start(self):
        threading.Thread(target=self._run, name="contact-relay", daemon=True).start()

    def stop(self):
        self._stop.set()
