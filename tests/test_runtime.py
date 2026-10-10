import os
import signal
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import runtime
from memtrace_harness.duties import _run_nightly_digest_pass
from memtrace_harness.memory_digest import PreferenceChanges


def _parts(service=None):
    scope = MagicMock()
    scope.name = "Beri"
    parts = MagicMock()
    parts.scope = scope
    parts.service = service or MagicMock()
    parts.trace_store = MagicMock()
    parts.trace_store.list_due_schedules.return_value = []
    parts.primary_session_mgr = MagicMock()
    return parts


def _config():
    config = MagicMock()
    config.shutdown_grace_seconds = 0
    config.schedule_timezone = "Asia/Taipei"
    config.approval_ttl_hours = 12
    return config


def _serve(parts, config=None, **intervals):
    kwargs = dict(scan_interval=9999, schedule_check_interval=9999)
    kwargs.update(intervals)
    with patch("memtrace_harness.runtime.build_harness", return_value=parts), patch(
        "memtrace_harness.runtime.HarnessApi"
    ) as api, patch("memtrace_harness.agy_mcp_check.check_antigravity_mcp", return_value=[]), patch(
        "memtrace_harness.chat_tools_check.check_chat_tools", return_value=[]
    ):
        runtime.serve(config or _config(), parts.scope, **kwargs)
    return api


class ServeLoopTests(TestCase):
    def test_it_starts_the_api_and_tells_the_operator_it_is_up(self) -> None:
        parts = _parts()

        def stop_after_first_tick():
            # Signal ourselves so the loop exits right after startup, without waiting on a real
            # OS-signal race.
            os.kill(os.getpid(), signal.SIGTERM)

        parts.service.drain_task_queues.side_effect = stop_after_first_tick
        api = _serve(parts)
        api.return_value.start.assert_called_once()
        api.return_value.stop.assert_called_once()
        parts.service.notify_operator.assert_called_once()
        (text,), _ = parts.service.notify_operator.call_args
        self.assertIn("已啟動", text)
        self.assertIn("Beri", text)

    def test_startup_problems_with_the_models_tooling_are_reported(self) -> None:
        parts = _parts()
        parts.service.drain_task_queues.side_effect = lambda: os.kill(os.getpid(), signal.SIGTERM)
        with patch("memtrace_harness.runtime.build_harness", return_value=parts), patch(
            "memtrace_harness.runtime.HarnessApi"
        ), patch("memtrace_harness.agy_mcp_check.check_antigravity_mcp", return_value=["agy is off"]), patch(
            "memtrace_harness.chat_tools_check.check_chat_tools", return_value=["no memtrace for codex"]
        ):
            runtime.serve(_config(), parts.scope, scan_interval=9999, schedule_check_interval=9999)
        said = " ".join(c.args[0] for c in parts.service.notify_operator.call_args_list)
        self.assertIn("agy is off", said)
        self.assertIn("no memtrace for codex", said)

    def test_it_triggers_due_schedules(self) -> None:
        due_row = {
            "id": "sched_abc123", "project": "Beri", "goal": "daily backlog review", "kind": "interval",
            "interval_seconds": 3600, "time_of_day": None, "audience": "tg:12345",
        }
        parts = _parts()

        def fake_list_due(now):
            os.kill(os.getpid(), signal.SIGTERM)
            return [due_row]

        parts.trace_store.list_due_schedules.side_effect = fake_list_due
        _serve(parts, schedule_check_interval=1)
        parts.service.run_due_schedule.assert_called_once_with(due_row)
        parts.trace_store.mark_schedule_ran.assert_called_once()
        self.assertEqual(parts.trace_store.mark_schedule_ran.call_args.kwargs["status"], "triggered")

    def test_a_due_schedule_past_its_window_end_is_skipped(self) -> None:
        # A weekdays 09:00-13:25 schedule that is somehow still "due" well after its window closed
        # (e.g. the Harness was down over lunch) must not start a run — it rolls forward and skips.
        due_row = {
            "id": "sched_window1", "project": "Beri", "goal": "morning backlog review", "kind": "weekdays",
            "interval_seconds": None, "time_of_day": "09:00", "end_time_of_day": "13:25",
            "audience": "tg:12345",
        }
        parts = _parts()

        def fake_list_due(now):
            os.kill(os.getpid(), signal.SIGTERM)
            return [due_row]

        parts.trace_store.list_due_schedules.side_effect = fake_list_due
        with patch("memtrace_harness.runtime.datetime") as mock_datetime:
            # 2026-09-22 (Tuesday) 14:00 Asia/Taipei == 06:00 UTC — past the 13:25 window end.
            mock_datetime.now.return_value = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            _serve(parts, schedule_check_interval=1)
        parts.service.run_due_schedule.assert_not_called()
        self.assertEqual(parts.trace_store.mark_schedule_ran.call_args.kwargs["status"], "skipped_window")

    def test_one_failing_duty_does_not_stop_the_others(self) -> None:
        parts = _parts()
        parts.service.drain_task_queues.side_effect = RuntimeError("boom")
        parts.service.process_job_requests.side_effect = lambda: os.kill(os.getpid(), signal.SIGTERM)
        _serve(parts)
        parts.service.process_order_intents.assert_called_once()
        parts.service.process_job_requests.assert_called_once()


class NightlyPreferenceNotificationTests(TestCase):
    def test_the_operator_is_told_what_the_harness_adopted_and_retired(self) -> None:
        changes = PreferenceChanges(
            adopted=[{"id": 7, "text": "回覆一律用繁體中文"}],
            retired=[{"id": 3, "text": "回覆簡短", "retire_reason": "2026-09-30 被 #7 取代"}],
            waiting=[{"id": 8, "text": "多的一條"}],
        )
        service = MagicMock()
        with patch("memtrace_harness.duties.run_memory_digests", return_value={"proj": changes}):
            _run_nightly_digest_pass(MagicMock(), MagicMock(), None, [], service=service)
        message = service.notify_operator.call_args.args[0]
        self.assertIn("- [#7] 回覆一律用繁體中文", message)
        self.assertIn("撤回 [#3] 回覆簡短（2026-09-30 被 #7 取代）", message)
        self.assertIn("另有 1 條超過每日自動採用上限", message)
        self.assertIn("直接在聊天裡說", message)

    def test_a_night_with_no_preference_changes_sends_nothing(self) -> None:
        service = MagicMock()
        with patch("memtrace_harness.duties.run_memory_digests", return_value={"proj": PreferenceChanges()}):
            _run_nightly_digest_pass(MagicMock(), MagicMock(), None, [], service=service)
        service.notify_operator.assert_not_called()

    def test_the_change_callback_fires_so_the_status_page_redraws(self) -> None:
        seen = []
        with patch("memtrace_harness.duties.run_memory_digests", return_value={}):
            _run_nightly_digest_pass(MagicMock(), MagicMock(), None, [], service=None, on_change=lambda: seen.append(1))
        self.assertEqual(seen, [1])
