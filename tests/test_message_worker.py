"""A plain chat message is answered at once; more from the same person before the reply goes out
cancels that reply and is answered together; anything else keeps its own turn."""

import threading
import time
from types import SimpleNamespace
from unittest import TestCase

from memtrace_harness.api import ChangeBus, MessageWorker
from memtrace_harness.service import ChatInterrupted


class FakeService:
    """handle_message 'thinks' until released, and honors a supersede the way the real chat path does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None, str]] = []
        self.finished: list[str] = []
        self.progress: list[tuple[str, str]] = []
        self.chat_superseded = lambda: False
        self.chat_generating = False
        self.release = threading.Event()
        self.started = threading.Event()
        self.hold = True
        self.triage = SimpleNamespace(
            triage_message=lambda text: SimpleNamespace(kind="status" if text.startswith("/") else "chat")
        )

    def handle_message(self, text, *, audience, reply_to_ref=None, reply_to_text=None, earlier_text="", attachments=None):
        self.calls.append((text, audience, reply_to_ref, earlier_text))
        self.attachments_seen = getattr(self, "attachments_seen", [])
        self.attachments_seen.append(attachments)
        self.started.set()
        self.chat_generating = True
        try:
            # A cancelled model call returns early; stand in for that by returning once superseded.
            end = time.monotonic() + 2
            while self.hold and not self.release.is_set() and not self.chat_superseded():
                if time.monotonic() > end:
                    raise AssertionError("never released")
                time.sleep(0.005)
        finally:
            self.chat_generating = False
        if self.chat_superseded():
            raise ChatInterrupted()
        self.finished.append(f"{earlier_text}|{text}")

    def send_message(self, audience, text):
        pass

    def report_chat_wait(self, text, audience):
        self.progress.append((text, audience))


class MessageWorkerTests(TestCase):
    def _worker(self):
        service = FakeService()
        worker = MessageWorker(service, ChangeBus())
        self.addCleanup(worker.stop)
        return worker, service

    def _wait(self, predicate, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_a_single_message_is_handled_immediately(self) -> None:
        worker, service = self._worker()
        service.hold = False
        worker.submit({"text": "你好", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: service.finished == ["|你好"]))

    def test_a_follow_up_before_the_reply_goes_out_is_answered_together_with_the_first(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "先看一下", "audience": "tg:1"})
        self.assertTrue(service.started.wait(2))
        worker.submit({"text": "還有這個", "audience": "tg:1"})
        service.release.set()
        self.assertTrue(self._wait(lambda: service.finished))
        self.assertEqual(service.finished, ["先看一下|還有這個"])
        self.assertEqual(service.calls[1][0], "還有這個")

    def test_a_command_waits_its_turn_and_is_not_merged(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "一般訊息", "audience": "tg:1"})
        self.assertTrue(service.started.wait(2))
        worker.submit({"text": "/status", "audience": "tg:1"})
        service.release.set()
        self.assertTrue(self._wait(lambda: len(service.finished) == 2))
        self.assertEqual([c[0] for c in service.calls], ["一般訊息", "/status"])

    def test_a_progress_only_follow_up_does_not_restart_the_lookup(self) -> None:
        from unittest.mock import patch

        worker, service = self._worker()
        worker.submit({"text": "觀察對話紀錄", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: service.chat_generating))
        with patch("memtrace_harness.api.kill_process_for_thread") as kill:
            worker.submit({"text": "有在處理嗎？", "audience": "tg:1"})
        kill.assert_not_called()
        self.assertEqual(service.progress, [("有在處理嗎？", "tg:1")])
        self.assertFalse(service.chat_superseded())
        service.release.set()
        self.assertTrue(self._wait(lambda: service.finished == ["|觀察對話紀錄"]))
        self.assertEqual(len(service.calls), 1)

    def test_a_progress_question_with_new_instructions_still_reaches_the_model(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "先看一下", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: service.chat_generating))
        worker.submit({"text": "有在處理嗎？順便看 Eric", "audience": "tg:1"})
        service.release.set()
        self.assertTrue(self._wait(lambda: service.finished))
        self.assertEqual(service.progress, [])
        self.assertIn("順便看 Eric", service.calls[-1][0])

    def test_another_audience_progress_query_does_not_expose_the_current_lookup(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "先看一下", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: service.chat_generating))
        worker.submit({"text": "有在處理嗎？", "audience": "tg:2"})
        self.assertEqual(service.progress, [])
        self.assertFalse(service.chat_superseded())
        service.release.set()
        self.assertTrue(self._wait(lambda: len(service.finished) == 2))

    def test_a_progress_query_while_idle_remains_a_normal_message(self) -> None:
        worker, service = self._worker()
        service.hold = False
        worker.submit({"text": "有在處理嗎？", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: service.finished == ["|有在處理嗎？"]))
        self.assertEqual(service.progress, [])

    def test_a_reply_to_a_message_and_another_audience_are_not_merged(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "甲", "audience": "tg:1"})
        self.assertTrue(service.started.wait(2))
        worker.submit({"text": "回這一則", "audience": "tg:1", "reply_to_ref": "tg:1:9"})
        worker.submit({"text": "乙", "audience": "tg:2"})
        service.release.set()
        self.assertTrue(self._wait(lambda: len(service.finished) == 3))
        self.assertEqual([c[0] for c in service.calls], ["甲", "回這一則", "乙"])

    def test_it_stops_interrupting_after_the_restart_limit(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "m0", "audience": "tg:1"})
        sent = 1
        # Each follow-up cancels the reply being made, until the limit is reached.
        for _ in range(MessageWorker.MAX_RESTARTS):
            self.assertTrue(self._wait(lambda: len(service.calls) == sent))
            worker.submit({"text": f"m{sent}", "audience": "tg:1"})
            sent += 1
        self.assertTrue(self._wait(lambda: len(service.calls) == sent))
        # One more: no longer interrupts, so this reply finishes and the newcomer is its own turn.
        worker.submit({"text": "late", "audience": "tg:1"})
        time.sleep(0.1)
        self.assertEqual(service.finished, [])
        service.release.set()
        self.assertTrue(self._wait(lambda: len(service.finished) == 2))
        self.assertEqual(service.calls[-1][0], "late")

    def test_cancel_drops_the_reply_being_made_instead_of_redoing_it_and_is_then_handled(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "幫我做個很久的事", "audience": "tg:1"})
        self.assertTrue(service.started.wait(2))
        worker.submit({"text": "/cancel", "audience": "tg:1"})
        self.assertTrue(self._wait(lambda: [c[0] for c in service.calls] == ["幫我做個很久的事", "/cancel"]))
        service.release.set()
        self.assertTrue(self._wait(lambda: service.finished == ["|/cancel"]))
        self.assertEqual([c[0] for c in service.calls], ["幫我做個很久的事", "/cancel"])  # the first was never answered

    def test_files_sent_in_separate_messages_of_one_burst_all_reach_the_one_answer(self) -> None:
        worker, service = self._worker()
        worker.submit({"text": "（傳了附件）", "audience": "tg:1", "attachments": [{"name": "a.jpg"}]})
        self.assertTrue(service.started.wait(2))
        worker.submit({"text": "（傳了附件）", "audience": "tg:1", "attachments": [{"name": "b.jpg"}]})
        service.release.set()
        self.assertTrue(self._wait(lambda: service.finished))
        self.assertEqual(service.attachments_seen[-1], [{"name": "a.jpg"}, {"name": "b.jpg"}])
