from __future__ import annotations

import threading
import time
from typing import Any


class InFlightTracker:
    """Tracks background daemon threads currently running an Agent Loop, so the
    gateway's shutdown sequence can wait for them (or, past a grace period, at least
    force-release their workspace locks) instead of the process just vanishing out
    from under them. Daemon threads get no cleanup guarantee when the interpreter
    exits — their `finally` blocks (which release the workspace lock) may never run —
    so without this, killing the process mid-run leaves that workspace locked forever
    until someone manually clears it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, dict[str, Any]] = {}

    def register(
        self, thread: threading.Thread, *, workspace_id: str, conversation_id: str, trace_store: Any
    ) -> None:
        assert thread.ident is not None, "register() must be called after thread.start()"
        with self._lock:
            self._entries[thread.ident] = {
                "thread": thread,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "trace_store": trace_store,
                "started_at": time.time(),
            }

    def unregister(self, thread: threading.Thread) -> None:
        with self._lock:
            self._entries.pop(thread.ident, None)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries.values())

    def wait_for_drain(self, timeout_seconds: float, *, poll_interval: float = 2.0) -> bool:
        """Block until every registered thread has unregistered itself (i.e. its
        background run finished and released its own lock normally), or the timeout
        elapses. Returns True if fully drained, False if entries are still in flight
        when the timeout hits."""
        deadline = time.monotonic() + timeout_seconds
        while self.snapshot() and time.monotonic() < deadline:
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        return not self.snapshot()


# One tracker per process — every background Agent Loop thread across the gateway
# (chat-triggered tasks, approval resumes, unattended-scanner backlog runs) registers
# into this single instance so the shutdown sequence has one place to check.
default_tracker = InFlightTracker()
