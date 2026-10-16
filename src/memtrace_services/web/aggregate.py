"""Putting the status of several Harnesses on one page."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from memtrace_services.client import HarnessClient, HarnessError, HarnessUnavailable

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15.0
# A page asking again within this long gets the last answer (several tabs, SSE + page load).
CACHE_SECONDS = 1.0


@dataclass
class HarnessTarget:
    name: str
    url: str
    token: str

    def client(self) -> HarnessClient:
        return HarnessClient(self.url, self.token, timeout=10.0)


class Changes:
    """Version counter + condition: every SSE connection waits for it to move."""

    def __init__(self, on_publish=None) -> None:
        self._condition = threading.Condition()
        self._version = 0
        self._on_publish = on_publish

    def publish(self) -> None:
        # Before anyone is woken: whoever wakes up must read fresh data, not the answer cached
        # an instant ago (a save looked up the project's Harness, which cached the page's data
        # just before the change it announces — the page then showed the old settings until the
        # next heartbeat).
        if self._on_publish is not None:
            self._on_publish()
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait(self, last_seen: int, timeout: float) -> int:
        with self._condition:
            self._condition.wait_for(lambda: self._version != last_seen, timeout=timeout)
            return self._version


class Aggregator:
    def __init__(self, targets: list[HarnessTarget]) -> None:
        self.targets = targets
        self._cache: tuple[float, dict] | None = None
        self._lock = threading.Lock()
        self.changes = Changes(on_publish=self._invalidate)
        self._stop = threading.Event()

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # ---- reading
    def collect(self) -> dict[str, Any]:
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
        data = self._collect_uncached()
        with self._lock:
            self._cache = (time.monotonic(), data)
        return data

    def _collect_uncached(self) -> dict[str, Any]:
        harnesses: list[dict] = []
        projects: list[dict] = []
        preferences: dict[str, list] = {"pending": [], "adopted": [], "retired": []}
        shared_stores: dict[str, dict] = {}
        execution_stores: dict[str, dict] = {}
        category_labels: dict = {}
        known: dict[str, list[str]] = {}
        for target in self.targets:
            try:
                status = target.client().status()
            except (HarnessUnavailable, HarnessError) as exc:
                harnesses.append({"name": target.name, "up": False, "error": str(exc)})
                continue
            info = status.get("harness", {})
            sources = status.get("execution_sources", {})
            if sources.get("available") and sources.get("store_id") not in execution_stores:
                execution_stores[sources["store_id"]] = {**sources, "harness": target.name}
            shared = status.get("shared_memory", {})
            if shared.get("available") and shared.get("store_id") not in shared_stores:
                shared_stores[shared["store_id"]] = {**shared, "harness": target.name}
            harnesses.append({"name": target.name, "up": True, "error": None, **info})
            projects += [{**p, "harness": target.name} for p in status.get("projects", [])]
            prefs = status.get("preferences", {})
            for bucket in preferences:
                for rule in prefs.get(bucket, []):
                    preferences[bucket].append({**rule, "harness": target.name, "uid": f"{target.name}:{rule['id']}"})
            category_labels = prefs.get("category_labels", category_labels)
            for provider, models in (status.get("known_models") or {}).items():
                merged = known.setdefault(provider, [])
                merged += [m for m in models if m not in merged]
        return {
            "harnesses": harnesses,
            "projects": projects,
            "preferences": {**preferences, "category_labels": category_labels},
            "shared_memory": list(shared_stores.values()),
            "execution_sources": list(execution_stores.values()),
            "known_models": known,
        }

    # ---- routing: a project's name finds the Harness that owns it
    def target_for(self, project: str) -> HarnessTarget | None:
        for item in self.collect()["projects"]:
            if item["name"] == project:
                return next((t for t in self.targets if t.name == item["harness"]), None)
        # A Harness that is down has no project to look up; a single configured Harness is unambiguous.
        return next((t for t in self.targets if t.name == project), self.targets[0] if len(self.targets) == 1 else None)

    # ---- change feed
    def start(self) -> None:
        for target in self.targets:
            threading.Thread(target=self._watch, args=(target,), name=f"watch-{target.name}", daemon=True).start()
        threading.Thread(target=self._heartbeat, name="status-heartbeat", daemon=True).start()

    def _watch(self, target: HarnessTarget) -> None:
        version = -1
        client = target.client()
        while not self._stop.is_set():
            try:
                new_version = client.changes(version, wait=30.0)
                if new_version != version:
                    version = new_version
                    self.changes.publish()
            except (HarnessUnavailable, HarnessError):
                self._stop.wait(5.0)
            except Exception:
                logger.exception(f"watching {target.name} failed")
                self._stop.wait(5.0)

    def _heartbeat(self) -> None:
        # Fallback refresh even with no activity, so a page never looks stalled.
        while not self._stop.wait(HEARTBEAT_SECONDS):
            self.changes.publish()

    def stop(self) -> None:
        self._stop.set()
