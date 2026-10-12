"""The page's chat box: talks to a Harness the way the Telegram service does.

A message typed on the page is posted to the Harness with audience `web:main`. Whatever the Harness
says back to that audience arrives through its outbox, fetched here as consumer "web" (Telegram never
sees those events, and never delivers them). Replies are kept in memory, per Harness, so the page can
long-poll for them; a restart of this service starts the conversation view empty (the Harness has the
real history). Things the Harness says to nobody in particular (audience None) still go to Telegram
only."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from memtrace_services.client import HarnessError, HarnessUnavailable
from memtrace_services.web.aggregate import HarnessTarget

logger = logging.getLogger(__name__)

AUDIENCE = "web:main"
KEEP_MESSAGES = 200
TYPING_SECONDS = 90.0
FETCH_WAIT_SECONDS = 25.0
RETRY_SECONDS = 5.0


class _Conversation:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.messages: list[dict[str, Any]] = []
        self.seq = 0
        self.typing_until = 0.0

    def add(self, role: str, text: str, *, actions=None, ref: str | None = None) -> dict:
        with self.cond:
            self.seq += 1
            message = {"id": self.seq, "role": role, "text": text, "actions": actions, "ref": ref, "rev": self.seq}
            self.messages.append(message)
            del self.messages[:-KEEP_MESSAGES]
            if role == "harness":
                self.typing_until = 0.0
            self.cond.notify_all()
            return message

    def touch(self, ref: str | None, *, text: str | None = None, actions="keep") -> None:
        """Change a delivered message in place (an edit or a removed keyboard). A changed message
        gets a new `rev`, which is how a page that already has it learns to redraw it."""
        with self.cond:
            for message in self.messages:
                if message["ref"] == ref:
                    if text is not None:
                        message["text"] = text
                    if actions != "keep":
                        message["actions"] = actions
                    self.seq += 1
                    message["rev"] = self.seq
                    self.cond.notify_all()
                    return

    def snapshot(self, since: int) -> dict:
        with self.cond:
            return {
                "messages": [m for m in self.messages if m["rev"] > since],
                "version": self.seq,
                "typing": time.monotonic() < self.typing_until,
            }

    def wait(self, since: int, timeout: float) -> dict:
        with self.cond:
            self.cond.wait_for(lambda: self.seq > since, timeout=timeout)
        return self.snapshot(since)


class ChatHub:
    def __init__(self, targets: list[HarnessTarget]) -> None:
        self._targets = {t.name: t for t in targets}
        self._conversations = {t.name: _Conversation() for t in targets}
        self._stop = threading.Event()

    def start(self) -> None:
        for name in self._targets:
            threading.Thread(target=self._consume, args=(name,), name=f"chat-{name}", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def known(self, harness: str) -> bool:
        return harness in self._targets

    # ---- page -> Harness
    def send(self, harness: str, text: str) -> None:
        conversation = self._conversations[harness]
        self._targets[harness].client().post_message(text, audience=AUDIENCE)
        conversation.add("user", text)
        with conversation.cond:
            conversation.typing_until = time.monotonic() + TYPING_SECONDS

    def act(self, harness: str, action_id: str, ref: str | None) -> str | None:
        return self._targets[harness].client().post_action(action_id, audience=AUDIENCE, message_ref=ref)

    def poll(self, harness: str, since: int, wait: float) -> dict:
        conversation = self._conversations[harness]
        return conversation.wait(since, wait) if wait > 0 else conversation.snapshot(since)

    # ---- Harness -> page
    def _consume(self, name: str) -> None:
        client = self._targets[name].client()
        conversation = self._conversations[name]
        while not self._stop.is_set():
            try:
                events = client.events(limit=20, wait=FETCH_WAIT_SECONDS, consumer="web")
                for event in events:
                    refs: list[str] = []
                    try:
                        refs = self._apply(conversation, event)
                    except Exception:
                        logger.exception(f"showing outbox event {event.get('id')} failed")
                    client.ack(event["id"], ok=True, message_refs=refs)
            except (HarnessUnavailable, HarnessError):
                self._stop.wait(RETRY_SECONDS)
            except Exception:
                logger.exception(f"fetching chat events from {name} failed")
                self._stop.wait(RETRY_SECONDS)

    @staticmethod
    def _apply(conversation: _Conversation, event: dict) -> list[str]:
        kind = event.get("kind")
        if kind == "message":
            with conversation.cond:
                ref = f"web:{conversation.seq + 1}"
            conversation.add("harness", event.get("text") or "", actions=event.get("actions") or None, ref=ref)
            return [ref]
        if kind == "edit":
            conversation.touch(event.get("ref"), text=event.get("text") or "", actions=event.get("actions") or None)
        elif kind == "clear":
            conversation.touch(event.get("ref"), actions=None)
        elif kind == "typing":
            with conversation.cond:
                conversation.typing_until = time.monotonic() + TYPING_SECONDS
                conversation.seq += 1
                conversation.cond.notify_all()
        return []
