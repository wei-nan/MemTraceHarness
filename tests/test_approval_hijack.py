"""A schedule run's needs_human is a notification, and a swipe-reply only resumes a task when it
is an answer (2026-10-05: a monitoring report's "needs a decision" opened an approval; the next
human reply resumed that monitoring run with a development goal, and a question typed as a
swipe-reply started an Opus planner run)."""
from __future__ import annotations

import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.loop import AgentLoopRunner, operational_task
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore
from tests.test_loop import QueueAdapter
from tests.test_telegram_gateway import _build_schedule_gateway


def _adapters(controller):
    adapters = {
        role: QueueAdapter(role, "codex", "m", [])
        for role in ("planner", "planner-escalation", "red-team", "developer")
    }
    adapters["controller"] = QueueAdapter("controller", "codex", "m", controller)
    return adapters


class ScheduleNeedsHumanIsOnlyANotificationTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.task = TaskEnvelope(task_id="t", workspace_id="ws", goal="check quotes", risk_level="low")

    def _stop(self, **kwargs):
        approvals = MagicMock()
        summary = AgentLoopRunner(
            adapters=_adapters([{"action": "stop", "reason": "needs a person"}]),
            trace_store=self.store,
            approval_manager=approvals,
            **kwargs,
        ).run(self.task)
        return summary, approvals

    def test_a_normal_run_still_opens_an_approval_when_it_needs_a_human(self) -> None:
        summary, approvals = self._stop()
        self.assertEqual(summary.status, "needs_human")
        approvals.request_approval.assert_called_once()

    def test_a_schedule_run_opens_no_approval(self) -> None:
        summary, approvals = self._stop(create_approvals=False)
        self.assertEqual(summary.status, "needs_human")
        approvals.request_approval.assert_not_called()

    def test_the_operational_prompt_reserves_needs_human_for_work_it_could_not_do(self) -> None:
        goal = operational_task(self.task).goal
        self.assertIn("ONLY when you could not do the work", goal)
        self.assertIn("'completed'", goal)


class ScheduleReportGatewayTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gateway, self.store, self.scope = _build_schedule_gateway(Path(self._tmp.name))
        self.gateway.send_message = MagicMock()

    def _run(self, schedule_id):
        summary = MagicMock(
            conversation_id="c1", status="needs_human", recommendation="公式可能有問題", stages=[]
        )
        self.gateway._run_new_task = MagicMock(return_value=summary)
        self.gateway.send_message.reset_mock()
        self.store.acquire_task_slot(self.scope.workspace_id, "c1", max_slots=3)
        self.gateway._launch_task(self.scope, "c1", "查報價", 12345, schedule_id)
        for t in threading.enumerate():
            if t.name.startswith("agent-loop-"):
                t.join(timeout=5)
        return self.gateway.send_message.call_args.args[1]

    def test_a_schedule_that_needs_attention_is_reported_as_a_notification(self) -> None:
        text = self._run("sched_75c5981703")
        self.assertIn("sched_75c5981703", text)
        self.assertIn("這只是通知", text)
        self.assertIn("公式可能有問題", text)
        self.assertNotIn("✅", text)

    def test_a_chat_task_that_needs_a_human_is_reported_as_before(self) -> None:
        text = self._run(None)
        self.assertIn("✅", text)
        self.assertNotIn("這只是通知", text)

    def test_only_schedule_runs_are_told_not_to_open_approvals(self) -> None:
        for schedule_id, expected in (("sched_x", False), (None, True)):
            with self.subTest(schedule_id=schedule_id), patch(
                "memtrace_harness.telegram_gateway.load_role_profiles", return_value={}
            ), patch(
                "memtrace_harness.telegram_gateway.build_role_adapter_candidates", return_value={}
            ), patch("memtrace_harness.telegram_gateway.AgentLoopRunner") as runner:
                runner.return_value.run.return_value = MagicMock(
                    conversation_id="c", status="succeeded", recommendation="ok"
                )
                self.gateway._run_new_task = type(self.gateway)._run_new_task.__get__(self.gateway)
                self.gateway._run_new_task(self.scope, "c", "g", schedule_id=schedule_id)
                self.assertEqual(runner.call_args.kwargs["create_approvals"], expected)


class SwipeReplyIsOnlyAnAnswerWhenTheModelSaysSoTests(TestCase):
    """A swipe-reply to a paused task's question goes to the chat model; only an answer resumes it."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gateway, self.store, self.scope = _build_schedule_gateway(Path(self._tmp.name))
        self.sent: list[str] = []
        self.gateway.send_message = lambda chat_id, text: self.sent.append(text) or True
        self.gateway._resume_approved_conversation = MagicMock()
        self.gateway.clear_message_keyboard = MagicMock()
        self.approvals = self.gateway.approval_manager
        self.req = self.approvals.request_approval(
            conversation_id="conv_paused",
            workspace=self.scope.workspace_id,
            working_directory=str(self.scope.working_directory),
            reason="ambiguous_requirement",
            proposed_action="要持久化已觸發但尚未送單的事件嗎？",
            resume_goal="改用新公式",
        )
        self.approvals.record_telegram_message(self.req.id, chat_id=12345, message_id=901)
        self.prompts: list[str] = []

    def _swipe(self, text: str, model_stdout: str | None, *, reply_to: int | None = 901):
        from memtrace_harness.cli_process import ProcessResult

        def fake_run(self_runner, command, **kwargs):
            self.prompts.append(command[-1])
            return ProcessResult(
                command=command, return_code=0 if model_stdout is not None else 1,
                stdout=model_stdout or "", stderr="boom",
                started_at="2026-10-05T00:00:00+00:00", completed_at="2026-10-05T00:00:01+00:00",
                duration_ms=1,
            )

        message = {"chat": {"id": 12345}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to, "text": "要持久化…嗎？"}
        with patch("urllib.request.urlopen"), patch(
            "memtrace_harness.cli_process.CliProcessRunner.run", fake_run
        ):
            return self.gateway.process_update({"update_id": 1, "message": message})

    def _status(self) -> str:
        return self.approvals.get_request(self.req.id).status

    def test_a_reply_the_model_recognises_as_an_answer_resumes_the_task_with_it(self) -> None:
        self._swipe("要，順便寫進狀態檔", "了解，會一併處理。\nHARNESS_APPROVAL_ANSWER::要持久化，並寫進狀態檔")
        self.assertEqual(self._status(), "approved")
        self.gateway._resume_approved_conversation.assert_called_once()
        self.assertEqual(
            self.gateway._resume_approved_conversation.call_args.args[1], "要持久化，並寫進狀態檔"
        )
        self.assertFalse(any("HARNESS_APPROVAL_ANSWER" in text for text in self.sent))   # never shown
        self.assertTrue(any("了解，會一併處理" in text for text in self.sent))

    def test_a_question_only_gets_an_answer_and_the_task_stays_paused(self) -> None:
        self._swipe("我確認一下這個 6% 公式是寫在 API 內還是我們定義的？", "是我們自己定義的，不在 API 裡。")
        self.assertEqual(self._status(), "pending")
        self.gateway._resume_approved_conversation.assert_not_called()
        self.assertTrue(any("我們自己定義" in text for text in self.sent))

    def test_the_model_is_told_which_question_this_is_and_to_hold_back_when_unsure(self) -> None:
        self._swipe("為什麼要持久化？", "因為…")
        prompt = self.prompts[0]
        self.assertIn(self.req.id, prompt)
        self.assertIn("HARNESS_APPROVAL_ANSWER::", prompt)
        self.assertIn("不確定時寧可不輸出", prompt)

    def test_a_second_answer_for_the_same_question_does_not_resume_twice(self) -> None:
        self._swipe("要", "好。\nHARNESS_APPROVAL_ANSWER::要")
        self._swipe("要", "好。\nHARNESS_APPROVAL_ANSWER::要")   # no longer pending: not even offered
        self.gateway._resume_approved_conversation.assert_called_once()

    def test_the_marker_is_ignored_when_the_message_did_not_reply_to_an_approval(self) -> None:
        self._swipe("要", "好。\nHARNESS_APPROVAL_ANSWER::要", reply_to=None)
        self.assertEqual(self._status(), "pending")
        self.gateway._resume_approved_conversation.assert_not_called()
        self.assertNotIn(self.req.id, self.prompts[0])

    def test_a_failed_model_call_leaves_the_task_paused_rather_than_guessing(self) -> None:
        self._swipe("要", None)
        self.assertEqual(self._status(), "pending")
        self.gateway._resume_approved_conversation.assert_not_called()

    def test_the_question_message_no_longer_promises_that_any_reply_resumes(self) -> None:
        text = self.req.format_telegram_message()
        self.assertIn("只有回答才會帶著答案繼續執行", text)
        self.assertNotIn("就會帶著答案繼續執行——", text)
