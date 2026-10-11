"""Chat messages sent close together are answered once; anything else keeps its own turn."""

import threading
import time
from types import SimpleNamespace
from unittest import TestCase

from memtrace_harness.api import ChangeBus, MessageWorker


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []
        self.done = threading.Event()
        self.expected = 1
        self.triage = SimpleNamespace(
            triage_message=lambda text: SimpleNamespace(kind="status" if text.startswith("/") else "chat")
        )

    def handle_message(self, text, *, audience, reply_to_ref=None, reply_to_text=None):
        self.calls.append((text, audience, reply_to_ref))
        if len(self.calls) >= self.expected:
            self.done.set()

    def send_message(self, audience, text):
        pass


class MessageWorkerTests(TestCase):
    def _worker(self, expected: int, quiet: float = 0.3) -> tuple[MessageWorker, FakeService]:
        service = FakeService()
        service.expected = expected
        worker = MessageWorker(service, ChangeBus())
        worker.COALESCE_QUIET_SECONDS = quiet
        self.addCleanup(worker.stop)
        return worker, service

    def test_messages_in_quick_succession_become_one_turn(self) -> None:
        worker, service = self._worker(expected=1)
        for text in ("先看一下", "還有這個", "以及那個"):
            worker.submit({"text": text, "audience": "tg:1"})
        self.assertTrue(service.done.wait(3))
        time.sleep(0.5)
        self.assertEqual(service.calls, [("先看一下\n\n還有這個\n\n以及那個", "tg:1", None)])

    def test_a_command_or_a_reply_is_never_merged(self) -> None:
        worker, service = self._worker(expected=3)
        worker.submit({"text": "一般訊息", "audience": "tg:1"})
        worker.submit({"text": "/status", "audience": "tg:1"})
        worker.submit({"text": "回這一則", "audience": "tg:1", "reply_to_ref": "tg:1:9"})
        self.assertTrue(service.done.wait(3))
        self.assertEqual(
            [c[0] for c in service.calls], ["一般訊息", "/status", "回這一則"]
        )

    def test_another_audience_is_not_merged_in(self) -> None:
        worker, service = self._worker(expected=2)
        worker.submit({"text": "甲", "audience": "tg:1"})
        worker.submit({"text": "乙", "audience": "tg:2"})
        self.assertTrue(service.done.wait(3))
        self.assertEqual([(c[0], c[1]) for c in service.calls], [("甲", "tg:1"), ("乙", "tg:2")])

    def test_no_window_means_no_waiting(self) -> None:
        worker, service = self._worker(expected=2, quiet=0)
        worker.submit({"text": "甲", "audience": "tg:1"})
        worker.submit({"text": "乙", "audience": "tg:1"})
        self.assertTrue(service.done.wait(3))
        self.assertEqual([c[0] for c in service.calls], ["甲", "乙"])
