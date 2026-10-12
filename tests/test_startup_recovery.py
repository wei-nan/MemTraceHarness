from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.schemas import TaskEnvelope
from tests.test_other_work import _sh
from tests.test_task_abandon_and_heartbeat import _service


def _task(goal: str) -> TaskEnvelope:
    return TaskEnvelope(task_id=f"task_{goal}", workspace_id="ws_t", goal=goal, context_refs=[], context_items=[],
                        constraints=[], risk_level="medium", source="chat")


class StartupRecoveryTests(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.service = _service(self.tmp)
        self.store = self.service.approval_manager.trace_store
        self.scope = self.service.projects[0]
        self.sent: list[str] = []
        self.service.notify_operator = self.sent.append  # type: ignore[method-assign]
        self.started = "9999-01-01T00:00:00+00:00"  # every row below predates this process

    def _conversation(self, name: str, *, goal: str, schedule_id: str | None = None) -> str:
        conv = self.store.create_conversation(_task(goal), conversation_id=name)
        self.store.create_run(_task(goal), conversation_id=conv)
        self.assertEqual(self.store.acquire_task_slot("ws_t", conv, max_slots=3, schedule_id=schedule_id), "acquired")
        return conv

    def _state(self, conv: str) -> str:
        return next(c["state"] for c in self.store.list_conversations_for_overview("ws_t", "1970", 50) if c["id"] == conv)

    def test_an_interrupted_task_is_stopped_marked_failed_released_and_reported_once(self) -> None:
        conv = self._conversation("chat_a", goal="修 #194")
        repo = self.tmp / "repo"
        repo.mkdir()
        _sh(repo, "git", "init", "-q", "-b", "main")
        (repo / "a.txt").write_text("1")
        _sh(repo, "git", "add", ".")
        _sh(repo, "git", "commit", "-q", "-m", "base")
        wt = repo / "wt"
        _sh(repo, "git", "worktree", "add", "-q", "-b", "harness/chat_a", str(wt))
        (wt / "b.txt").write_text("2")
        _sh(wt, "git", "add", ".")
        _sh(wt, "git", "commit", "-q", "-m", "work")
        self.store.record_conversation_worktree(conv, workspace_id="ws_t", path=str(wt), branch="harness/chat_a",
                                                base_branch="main", base_commit=None)
        with patch("memtrace_harness.service.kill_provider_clis_in_directory", return_value=[4242]) as kill:
            self.assertEqual(self.service.recover_interrupted_tasks(self.scope, self.started), 1)
            self.assertEqual(self.service.recover_interrupted_tasks(self.scope, self.started), 0)  # nothing left to do
        kill.assert_called_once_with(str(wt))
        self.assertEqual(self._state(conv), "failed")
        self.assertEqual(self.store.list_workspace_locks("ws_t"), [])
        (text,) = self.sent
        for part in ("chat_a", "修 #194", "harness/chat_a", "1 個 commit 還不在主線", "已終止", "沒有自動續跑"):
            self.assertIn(part, text)

    def test_a_task_running_in_this_process_or_started_after_it_is_left_alone(self) -> None:
        live = self._conversation("chat_live", goal="g")
        fresh = self._conversation("chat_fresh", goal="g")
        entry = {"conversation_id": live, "workspace_id": "ws_t", "started_at": 0.0}
        with patch("memtrace_harness.service.default_tracker.snapshot", return_value=[entry]):
            n = self.service.recover_interrupted_tasks(self.scope, "1970-01-01T00:00:00+00:00")
        self.assertEqual(n, 0)
        self.assertEqual({self._state(live), self._state(fresh)}, {"active"})
        self.assertEqual(len(self.store.list_workspace_locks("ws_t")), 2)
        self.assertEqual(self.sent, [])

    def test_a_schedule_run_is_released_quietly_so_its_schedule_can_fire_again(self) -> None:
        conv = self._conversation("chat_s", goal="每 5 分鐘檢查", schedule_id="sched_1")
        self.assertEqual(self.service.recover_interrupted_tasks(self.scope, self.started), 0)
        self.assertEqual(self._state(conv), "failed")
        self.assertFalse(self.store.schedule_in_flight("sched_1"))
        self.assertEqual(self.sent, [])

    def test_a_stale_lock_without_a_live_conversation_is_released(self) -> None:
        self.assertEqual(self.store.acquire_task_slot("ws_t", "orphan_lock", max_slots=3), "acquired")
        self.service.recover_interrupted_tasks(self.scope, self.started)
        self.assertEqual(self.store.list_workspace_locks("ws_t"), [])


class RestartScriptTests(TestCase):
    def test_the_script_refuses_to_restart_while_a_task_runs_unless_forced(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "scripts" / "restart-harness.sh").read_text()
        self.assertIn("--force", text)
        self.assertIn("workspace_locks", text)
        self.assertEqual(subprocess.run(["bash", "-n", str(Path(__file__).resolve().parent.parent / "scripts" / "restart-harness.sh")]).returncode, 0)
