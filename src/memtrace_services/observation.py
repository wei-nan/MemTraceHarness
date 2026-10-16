"""Relay only explicitly shared observations between local Harness HTTP APIs.

Reuses the web service's authenticated targets, never reads Harness files or model transports.
The coordinator stores cursors, coverage and evidence, so a service restart cannot lose its place.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from memtrace_services.client import HarnessError, HarnessUnavailable

logger = logging.getLogger(__name__)


class ObservationRelay:
    def __init__(self, targets):
        self.targets = {target.name: target for target in targets}
        self.stop_event = threading.Event()
        self.thread = None

    def collect_once(self):
        for coordinator in self.targets.values():
            try:
                control = coordinator.client()._request("GET", "/v1/improvement")
                if not control.get("enabled"):
                    continue
                states = {row["source"]: row for row in control.get("sources_state", [])}
                for name in control.get("sources", []):
                    previous = states.get(name, {})
                    if previous.get("status") == "ok" and time.time() - previous.get("collected_at", 0) < control.get("interval_seconds", 1800):
                        continue
                    target, cursor = self.targets.get(name), previous.get("cursor", 0)
                    status, events = "unreachable", []
                    if target is not None:
                        try:
                            snapshot = target.client()._request("GET", "/v1/observations?" + urllib.parse.urlencode({"after": cursor}))
                            status = "ok" if snapshot.get("sharing") else "sharing_disabled"
                            if status == "ok":
                                cursor, events = snapshot["cursor"], snapshot["events"]
                        except HarnessError as exc:
                            status = "upgrade_required" if exc.status == 404 else "unreachable"
                        except HarnessUnavailable:
                            pass
                    coordinator.client()._request("POST", "/v1/observations", {"source": name, "cursor": cursor, "events": events, "status": status})
            except (HarnessUnavailable, HarnessError):
                continue

    def start(self):
        def run():
            while not self.stop_event.is_set():
                try:
                    self.collect_once()
                except Exception:
                    logger.exception("observation relay failed")
                self.stop_event.wait(60)
        self.thread = threading.Thread(target=run, name="observation-relay", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
