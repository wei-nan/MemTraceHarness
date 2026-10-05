from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.loop import AgentLoopRunner, controller_task
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.worktrees import IntegrationReport, WorktreeManager
from tests.test_loop import (
    QueueAdapter,
    completed_development,
    passed_gate,
    ready_plan,
)
from tests.test_worktrees import git, make_repo


class WritingDeveloper(QueueAdapter):
    """A Developer that really edits files in the directory it was given."""

    def __init__(self, directory: Path, files: dict[str, str]) -> None:
        super().__init__("developer", "antigravity", "gemini", [completed_development()])
        self.directory = directory
        self.files = files

    def run(self, task, trace_id):
        for name, content in self.files.items():
            (self.directory / name).write_text(content)
        return super().run(task, trace_id)


class WorktreeLoopTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = make_repo(self.root)
        self.manager = WorktreeManager(self.root / "worktrees")
        self.store = TraceStore(self.root / "t.sqlite3")
        self.task = TaskEnvelope(
            task_id="t", workspace_id="ws", goal="Add a file", risk_level="medium"
        )

    def _run(self, converge: dict, files: dict[str, str], worktree=None):
        worktree = worktree or self.manager.create(self.repo, "proj", "chat_1")
        adapters = {
            "controller": QueueAdapter(
                "controller", "codex", "m", [{"action": "run_planner", "reason": "go"}, converge]
            ),
            "planner": QueueAdapter("planner", "claude", "sonnet", [ready_plan()]),
            "planner-escalation": QueueAdapter("planner-escalation", "claude", "opus", []),
            "red-team": QueueAdapter("red-team", "codex", "m", [passed_gate(), passed_gate()]),
            "developer": WritingDeveloper(worktree.path, files),
        }
        runner = AgentLoopRunner(
            adapters=adapters,
            trace_store=self.store,
            working_directory=worktree.path,
            worktree=worktree,
            worktree_manager=self.manager,
            repo_root=self.repo,
        )
        return runner.run(self.task, conversation_id="chat_1"), adapters, worktree

    def test_controller_is_told_about_the_pending_merge_and_may_merge_it(self) -> None:
        summary, adapters, worktree = self._run(
            {"action": "merge", "reason": "clean change"}, {"new.txt": "hi\n"}
        )

        self.assertEqual(summary.status, "succeeded")
        self.assertIn("Merged 'harness/chat_1' into 'main'", summary.recommendation)
        self.assertEqual((self.repo / "new.txt").read_text(), "hi\n")
        converge_task = adapters["controller"].calls[-1]
        self.assertIn("merge", converge_task.goal)
        snapshot = next(i for i in converge_task.context_items if i.content_type == "loop_snapshot")
        self.assertIn('"integration"', snapshot.body)
        self.assertIn("harness/chat_1", snapshot.body)
        self.assertIn("new.txt", snapshot.body)

    def test_finish_leaves_the_branch_unmerged_and_says_so(self) -> None:
        summary, _, worktree = self._run(
            {"action": "finish", "reason": "leave for review"}, {"new.txt": "hi\n"}
        )

        self.assertEqual(summary.status, "succeeded")
        self.assertIn("NOT been merged", summary.recommendation)
        self.assertFalse((self.repo / "new.txt").exists())
        self.assertEqual(self.manager.commits_ahead(self.repo, worktree), 1)

    def test_a_conflicting_merge_stops_for_a_human_and_keeps_the_work(self) -> None:
        # Both tasks start from the same commit; the other one merges first.
        mine = self.manager.create(self.repo, "proj", "chat_1")
        other = self.manager.create(self.repo, "proj", "chat_other")
        (other.path / "a.txt").write_text("from other\n")
        self.manager.commit_all(other, "other")
        self.assertTrue(self.manager.merge(self.repo, other, "merge other").ok)

        summary, _, worktree = self._run(
            {"action": "merge", "reason": "try"}, {"a.txt": "from this task\n"}, worktree=mine
        )

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("a.txt", summary.recommendation)
        self.assertIn("harness/chat_1", summary.recommendation)
        self.assertEqual((self.repo / "a.txt").read_text(), "from other\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_merge_is_not_offered_when_the_task_changed_nothing(self) -> None:
        report = IntegrationReport("harness/x", "main", 0, [], False, True, True, [])
        task = controller_task(self.task, stage="converge", stages=[], integration=report)
        self.assertNotIn("'merge'", task.goal)

        summary, _, _ = self._run({"action": "finish", "reason": "nothing to do"}, {})
        self.assertEqual(summary.status, "succeeded")
        self.assertNotIn("NOT been merged", summary.recommendation)

    def test_without_a_worktree_the_converge_stage_is_unchanged(self) -> None:
        task = controller_task(self.task, stage="converge", stages=[])
        self.assertIn("['finish', 'ask_human']", task.goal)


class SharedFolderEditLockTests(TestCase):
    """Tasks that share one folder (no worktree) take turns to develop; nothing else waits."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.task = TaskEnvelope(task_id="t", workspace_id="ws", goal="Edit", risk_level="medium")

    def _adapters(self, controller):
        return {
            "controller": QueueAdapter("controller", "codex", "m", controller),
            "planner": QueueAdapter("planner", "claude", "sonnet", [ready_plan()]),
            "planner-escalation": QueueAdapter("planner-escalation", "claude", "opus", []),
            "red-team": QueueAdapter("red-team", "codex", "m", [passed_gate(), passed_gate()]),
            "developer": QueueAdapter("developer", "antigravity", "g", [completed_development()]),
        }

    def _runner(self, adapters, results, **kwargs):
        calls = []

        def acquire():
            calls.append("acquire")
            return results.pop(0) if results else True

        return (
            AgentLoopRunner(
                adapters=adapters, trace_store=self.store,
                edit_lock=(acquire, lambda: calls.append("release")),
                edit_lock_poll_seconds=0, sleep=lambda _s: calls.append("wait"), **kwargs,
            ),
            calls,
        )

    def test_development_waits_for_the_other_task_then_proceeds_and_releases(self) -> None:
        adapters = self._adapters(
            [{"action": "run_planner", "reason": "go"}, {"action": "finish", "reason": "done"}]
        )
        runner, calls = self._runner(adapters, [False, False, True])
        summary = runner.run(self.task)
        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(calls, ["acquire", "wait", "acquire", "wait", "acquire", "release"])

    def test_it_gives_up_with_a_clear_message_if_the_other_task_never_finishes(self) -> None:
        adapters = self._adapters([{"action": "run_planner", "reason": "go"}])
        runner, calls = self._runner(adapters, [False] * 50, edit_lock_wait_seconds=0)
        summary = runner.run(self.task)
        self.assertEqual(summary.status, "needs_human")
        self.assertIn("Another development task", summary.recommendation)
        self.assertEqual(len(adapters["developer"].calls), 0)
        self.assertNotIn("release", calls)          # never held it, nothing to release

    def test_an_operational_task_never_asks_for_the_lock(self) -> None:
        adapters = self._adapters([{"action": "run_operational_action", "reason": "just check"}])
        runner, calls = self._runner(adapters, [False] * 50)
        summary = runner.run(self.task)
        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(calls, [])

    def test_the_lock_is_released_even_if_the_run_raises(self) -> None:
        adapters = self._adapters(
            [{"action": "run_planner", "reason": "go"}, {"action": "finish", "reason": "done"}]
        )
        adapters["developer"].run = MagicMock(side_effect=RuntimeError("boom"))
        runner, calls = self._runner(adapters, [True])
        with self.assertRaises(RuntimeError):
            runner.run(self.task)
        self.assertEqual(calls, ["acquire", "release"])

    def test_without_an_edit_lock_nothing_changes(self) -> None:
        adapters = self._adapters(
            [{"action": "run_planner", "reason": "go"}, {"action": "finish", "reason": "done"}]
        )
        summary = AgentLoopRunner(adapters=adapters, trace_store=self.store).run(self.task)
        self.assertEqual(summary.status, "succeeded")
