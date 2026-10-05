from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import threading
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.scope import DEFAULT_MAX_WORKERS, ProjectScope
from memtrace_harness.trace_store import TraceStore
from tests.test_telegram_gateway import _build_schedule_gateway
from tests.test_worktrees import git, make_repo


class TaskSlotTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")

    def test_a_workspace_holds_up_to_max_slots_at_once(self) -> None:
        results = [
            self.store.acquire_task_slot("ws", f"c{i}", max_slots=3) for i in range(4)
        ]
        self.assertEqual(results, ["acquired", "acquired", "acquired", "full"])
        self.assertEqual(len(self.store.list_workspace_locks("ws")), 3)
        # Another workspace has its own slots.
        self.assertEqual(self.store.acquire_task_slot("ws2", "x", max_slots=3), "acquired")

    def test_releasing_one_task_frees_exactly_its_slot(self) -> None:
        for i in range(3):
            self.store.acquire_task_slot("ws", f"c{i}", max_slots=3)
        self.store.release_workspace_lock("ws", "c1")
        self.assertEqual(
            [l["conversation_id"] for l in self.store.list_workspace_locks("ws")], ["c0", "c2"]
        )
        self.assertEqual(self.store.acquire_task_slot("ws", "c9", max_slots=3), "acquired")

    def test_release_without_a_conversation_clears_the_whole_workspace(self) -> None:
        self.store.acquire_task_slot("ws", "a", max_slots=3)
        self.store.acquire_task_slot("ws", "b", max_slots=3)
        self.store.release_workspace_lock("ws")
        self.assertIsNone(self.store.get_workspace_lock("ws"))

    def test_the_same_schedule_cannot_run_twice_but_another_can(self) -> None:
        self.assertEqual(
            self.store.acquire_task_slot("ws", "c1", max_slots=3, schedule_id="sch_w12345"),
            "acquired",
        )
        self.assertEqual(
            self.store.acquire_task_slot("ws", "c2", max_slots=3, schedule_id="sch_w12345"),
            "duplicate_schedule",
        )
        self.assertEqual(
            self.store.acquire_task_slot("ws", "c3", max_slots=3, schedule_id="sch_w67890"),
            "acquired",
        )
        # Once the first run ends, the schedule may fire again.
        self.store.release_workspace_lock("ws", "c1")
        self.assertEqual(
            self.store.acquire_task_slot("ws", "c4", max_slots=3, schedule_id="sch_w12345"),
            "acquired",
        )

    def test_a_schedule_still_waiting_in_the_queue_also_counts_as_in_flight(self) -> None:
        self.store.enqueue_task(
            workspace_id="ws", kind="new", payload={"goal": "g"}, schedule_id="sch_a", chat_id=1
        )
        self.assertTrue(self.store.schedule_in_flight("sch_a"))
        self.assertEqual(
            self.store.acquire_task_slot("ws", "c1", max_slots=3, schedule_id="sch_a"),
            "duplicate_schedule",
        )
        # The queue drain is starting that very entry, so it must not block itself.
        self.assertEqual(
            self.store.acquire_task_slot(
                "ws", "c1", max_slots=3, schedule_id="sch_a", check_queue=False
            ),
            "acquired",
        )

    def test_a_conversation_cannot_hold_two_slots(self) -> None:
        self.store.acquire_task_slot("ws", "c1", max_slots=3)
        self.assertEqual(self.store.acquire_task_slot("ws", "c1", max_slots=3), "already_running")

    def test_concurrent_claims_never_exceed_the_limit(self) -> None:
        outcomes: list[str] = []

        def claim(i: int) -> None:
            outcomes.append(self.store.acquire_task_slot("ws", f"c{i}", max_slots=3))

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("acquired"), 3)

    def test_the_queue_is_first_in_first_out_and_claimed_once(self) -> None:
        first = self.store.enqueue_task(workspace_id="ws", kind="new", payload={"goal": "1"}, chat_id=1)
        self.store.enqueue_task(workspace_id="ws", kind="new", payload={"goal": "2"}, chat_id=1)
        self.assertEqual(self.store.peek_next_queued_task("ws")["payload"], {"goal": "1"})
        self.assertTrue(self.store.claim_queued_task(first))
        self.assertFalse(self.store.claim_queued_task(first))
        self.assertEqual(self.store.peek_next_queued_task("ws")["payload"], {"goal": "2"})
        self.assertEqual(self.store.queued_workspace_ids(), ["ws"])

    def test_an_old_one_lock_per_workspace_table_is_migrated(self) -> None:
        path = Path(self._tmp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE workspace_locks (
                    workspace_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    locked_at TEXT NOT NULL
                );
                INSERT INTO workspace_locks VALUES ('ws', 'old_conv', '2026-01-01T00:00:00+00:00');
                """
            )
            conn.commit()

        store = TraceStore(path)

        self.assertEqual(store.get_workspace_lock("ws")["conversation_id"], "old_conv")
        self.assertEqual(store.acquire_task_slot("ws", "new_conv", max_slots=3), "acquired")
        self.assertEqual(len(store.list_workspace_locks("ws")), 2)


class ScopeWorkerSettingTests(TestCase):
    def _scope(self, extra: str = "") -> ProjectScope:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "harness-scope.md"
        path.write_text(f"- workspace_id: ws_test\n{extra}", encoding="utf-8")
        return ProjectScope.from_file(path)

    def test_defaults_to_three_workers(self) -> None:
        self.assertEqual(self._scope().max_workers, DEFAULT_MAX_WORKERS)
        self.assertEqual(DEFAULT_MAX_WORKERS, 3)

    def test_is_configurable_per_project(self) -> None:
        self.assertEqual(self._scope("- max_workers: 5\n").max_workers, 5)
        self.assertEqual(self._scope("- max_workers: 1\n").max_workers, 1)
        self.assertEqual(self._scope("- max_workers: 0\n").max_workers, 1)
        self.assertEqual(self._scope("- max_workers: lots\n").max_workers, 3)

    def test_worktree_setup_command_is_optional(self) -> None:
        self.assertIsNone(self._scope().worktree_setup_command)
        self.assertEqual(
            self._scope("- worktree_setup_command: ln -s $HARNESS_REPO_ROOT/node_modules .\n")
            .worktree_setup_command,
            "ln -s $HARNESS_REPO_ROOT/node_modules .",
        )


class GatewayTaskSchedulingTests(TestCase):
    def _gateway(self, max_workers: int = 3):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        gateway, store, scope = _build_schedule_gateway(Path(self._tmp.name))
        scope.max_workers = max_workers
        gateway.send_message = MagicMock()
        # The fixture folder isn't a git repo; pretend it can use worktrees so the
        # configured worker count applies (the real fallback is tested below).
        patcher = patch("memtrace_harness.telegram_gateway.can_use_worktrees", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Hold every started task open so slots stay taken until the test releases them.
        self.release = threading.Event()
        self.started: list[str] = []

        def fake_run(scope, conv_id, goal, schedule_id=None):
            self.started.append(goal)
            self.release.wait(timeout=10)
            summary = MagicMock(conversation_id=conv_id, status="succeeded", recommendation="ok")
            return summary

        gateway._run_new_task = fake_run
        gateway._report_run_outcome = MagicMock()
        gateway._model_summary = MagicMock(return_value="m")
        self.addCleanup(self.release.set)
        return gateway, store, scope

    def _join(self) -> None:
        # A finishing task starts the next queued one, so keep joining until none is left.
        self.release.set()
        for _ in range(20):
            alive = [t for t in threading.enumerate() if t.name.startswith("agent-loop-")]
            if not alive:
                return
            for t in alive:
                t.join(timeout=5)

    def _wait_started(self, count: int) -> None:
        for _ in range(200):
            if len(self.started) >= count:
                return
            threading.Event().wait(0.01)

    def test_three_tasks_run_at_once_and_the_fourth_is_queued_then_started(self) -> None:
        gateway, store, scope = self._gateway()
        for goal in ("t1", "t2", "t3", "t4"):
            gateway._start_or_queue_task(scope, goal, 12345)
        self._wait_started(3)

        self.assertEqual(sorted(self.started), ["t1", "t2", "t3"])
        self.assertEqual(len(store.list_workspace_locks(scope.workspace_id)), 3)
        queued = store.list_queued_tasks(scope.workspace_id)
        self.assertEqual([q["payload"]["goal"] for q in queued], ["t4"])
        self.assertIn("排入佇列", gateway.send_message.call_args_list[-1].args[1])

        self._join()
        self._wait_started(4)
        self._join()
        self.assertIn("t4", self.started)
        self.assertEqual(store.list_queued_tasks(scope.workspace_id), [])
        self.assertEqual(store.list_workspace_locks(scope.workspace_id), [])

    def test_with_one_worker_it_behaves_like_before_but_queues_the_second(self) -> None:
        gateway, store, scope = self._gateway(max_workers=1)
        gateway._start_or_queue_task(scope, "t1", 12345)
        gateway._start_or_queue_task(scope, "t2", 12345)
        self._wait_started(1)
        self.assertEqual(self.started, ["t1"])
        self.assertEqual(len(store.list_queued_tasks(scope.workspace_id)), 1)
        self._join()
        self._wait_started(2)
        self._join()
        self.assertEqual(self.started, ["t1", "t2"])

    def test_a_schedule_is_skipped_while_its_previous_run_is_unfinished(self) -> None:
        gateway, store, scope = self._gateway()
        first = store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="追蹤持股", kind="interval",
            interval_seconds=600, chat_id=12345, next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        other = store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="另一個", kind="interval",
            interval_seconds=600, chat_id=12345, next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

        gateway.run_due_schedule(store.get_schedule(first))
        self._wait_started(1)
        gateway.run_due_schedule(store.get_schedule(first))   # next cycle, still running
        gateway.run_due_schedule(store.get_schedule(other))   # a different schedule: fine
        self._wait_started(2)

        self.assertEqual(sorted(self.started), ["另一個", "追蹤持股"])
        self.assertEqual(store.list_queued_tasks(scope.workspace_id), [])
        skipped = [
            t for t in store.get_primary_session_turns("psess_test_proj")
            if "本輪略過" in t["content"]
        ]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["schedule_id"], first)
        self._join()

    def test_a_queued_schedule_run_is_not_queued_a_second_time(self) -> None:
        gateway, store, scope = self._gateway(max_workers=1)
        gateway._start_or_queue_task(scope, "blocker", 12345)
        self._wait_started(1)
        sched = store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="s", kind="interval",
            interval_seconds=600, chat_id=12345, next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        gateway.run_due_schedule(store.get_schedule(sched))   # queued behind the blocker
        gateway.run_due_schedule(store.get_schedule(sched))   # skipped: already waiting
        self.assertEqual(len(store.list_queued_tasks(scope.workspace_id)), 1)
        self._join()


class GatewayWorktreeTests(TestCase):
    def test_tasks_in_a_git_project_run_in_their_own_worktrees(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        repo = make_repo(root)
        gateway, store, scope = _build_schedule_gateway(root)
        scope.working_directory = repo

        first_dir, first = gateway._provision_workdir(scope, "chat_1")
        second_dir, second = gateway._provision_workdir(scope, "chat_2")
        again_dir, _ = gateway._provision_workdir(scope, "chat_1")

        self.assertNotEqual(first_dir, repo)
        self.assertNotEqual(first_dir, second_dir)
        self.assertEqual(again_dir, first_dir)        # a resumed conversation reuses it
        self.assertTrue(first_dir.is_dir())
        self.assertEqual(store.get_conversation_worktree("chat_1")["branch"], "harness/chat_1")

    def test_one_worker_or_a_non_git_project_keeps_working_in_place(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        gateway, store, scope = _build_schedule_gateway(root)   # root is not a git repo
        self.assertEqual(gateway._provision_workdir(scope, "chat_1"), (root, None))

        repo = make_repo(root)
        scope.working_directory = repo
        scope.max_workers = 1
        self.assertEqual(gateway._provision_workdir(scope, "chat_2"), (repo, None))

    def test_finishing_a_task_that_changed_nothing_removes_its_worktree(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        repo = make_repo(root)
        gateway, store, scope = _build_schedule_gateway(root)
        scope.working_directory = repo
        work_dir, worktree = gateway._provision_workdir(scope, "chat_1")

        note = gateway._finalize_worktree(scope, "chat_1", worktree, "succeeded")

        self.assertEqual(note, "")
        self.assertFalse(work_dir.exists())
        self.assertIsNone(store.get_conversation_worktree("chat_1"))

    def test_unmerged_work_and_stopped_tasks_keep_their_worktree(self) -> None:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        repo = make_repo(root)
        gateway, store, scope = _build_schedule_gateway(root)
        scope.working_directory = repo

        work_dir, worktree = gateway._provision_workdir(scope, "chat_1")
        (work_dir / "new.txt").write_text("x\n")
        note = gateway._finalize_worktree(scope, "chat_1", worktree, "succeeded")
        self.assertIn("harness/chat_1", note)
        self.assertIn("尚未合併", note)
        self.assertTrue(work_dir.is_dir())

        # needs_human: even with no changes the worktree stays, because the approval
        # that resumes this conversation points at it.
        stopped_dir, stopped = gateway._provision_workdir(scope, "chat_2")
        gateway._finalize_worktree(scope, "chat_2", stopped, "needs_human")
        self.assertTrue(stopped_dir.is_dir())


class ResumeSlotTests(TestCase):
    def _setup(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gateway, store, scope = _build_schedule_gateway(Path(tmp.name))
        patcher = patch("memtrace_harness.telegram_gateway.can_use_worktrees", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        gateway.notify_all_allowlisted = MagicMock()
        gateway._launch_resume = MagicMock()
        req = gateway.approval_manager.request_approval(
            conversation_id="chat_resume",
            workspace=scope.workspace_id,
            working_directory=str(scope.working_directory),
            reason="ambiguous_requirement",
            proposed_action="?",
            resume_goal="do it",
        )
        return gateway, store, scope, req

    def test_an_approval_waits_in_the_queue_when_every_slot_is_busy(self) -> None:
        gateway, store, scope, req = self._setup()
        for i in range(3):
            store.acquire_task_slot(scope.workspace_id, f"busy{i}", max_slots=3)

        gateway._resume_approved_conversation(req, "my answer")

        gateway._launch_resume.assert_not_called()
        [item] = store.list_queued_tasks(scope.workspace_id)
        self.assertEqual(item["kind"], "resume")
        self.assertEqual(item["conversation_id"], "chat_resume")
        self.assertEqual(item["payload"], {"request_id": req.id, "answer": "my answer"})

    def test_a_free_slot_starts_the_resume_immediately(self) -> None:
        gateway, store, scope, req = self._setup()
        gateway._resume_approved_conversation(req, None)
        gateway._launch_resume.assert_called_once_with(req, None)
        self.assertEqual(
            [l["conversation_id"] for l in store.list_workspace_locks(scope.workspace_id)],
            ["chat_resume"],
        )

    def test_a_slot_the_scanner_reserved_for_this_approval_is_handed_over(self) -> None:
        gateway, store, scope, req = self._setup()
        store.acquire_task_slot(scope.workspace_id, "chat_resume", max_slots=3)  # scanner's hold
        gateway._resume_approved_conversation(req, None)
        gateway._launch_resume.assert_called_once()
        self.assertEqual(store.list_queued_tasks(scope.workspace_id), [])

    def test_a_queued_resume_starts_when_a_slot_frees_up(self) -> None:
        gateway, store, scope, req = self._setup()
        for i in range(3):
            store.acquire_task_slot(scope.workspace_id, f"busy{i}", max_slots=3)
        gateway._resume_approved_conversation(req, "ans")
        gateway.approval_manager.respond(req.id, "clarify", 12345, "ans")

        store.release_workspace_lock(scope.workspace_id, "busy0")
        gateway.drain_task_queues()

        gateway._launch_resume.assert_called_once()
        self.assertEqual(gateway._launch_resume.call_args.args[1], "ans")
        self.assertEqual(store.list_queued_tasks(scope.workspace_id), [])


class TaiwanTradeChatNoticeTests(TestCase):
    def test_chat_model_is_told_about_the_tools_only_when_the_proxy_is_on(self) -> None:
        from memtrace_harness.telegram_gateway import TelegramGateway

        with patch.dict("os.environ", {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/keys/tw"}):
            notice = TelegramGateway._taiwantrade_notice()
        self.assertIn("get_balance", notice)
        self.assertIn("不要用 shell/curl", notice)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(TelegramGateway._taiwantrade_notice(), "")


class NoWorktreeFallbackTests(TestCase):
    """A project that cannot give each task its own worktree still gets all its worker slots;
    only development is serialized (by the edit lock)."""

    def _gateway(self, root: Path):
        gateway, store, scope = _build_schedule_gateway(root)
        scope.max_workers = 3
        gateway.send_message = MagicMock()
        return gateway, store, scope

    def test_a_non_git_project_still_gets_every_slot_and_works_in_place(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(Path(tmp))
            self.assertEqual(gateway._slot_limit(scope), 3)
            self.assertEqual(gateway._provision_workdir(scope, "c1"), (scope.working_directory, None))

    def test_a_git_repo_with_no_commit_yet_is_treated_the_same(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = root / "fresh"
            repo.mkdir()
            git(repo, "init", "-b", "main")
            (repo / "untracked.txt").write_text("x")
            gateway, store, scope = self._gateway(root)
            scope.working_directory = repo
            self.assertEqual(gateway._slot_limit(scope), 3)
            # No "cannot read HEAD" crash: it just works in place.
            self.assertEqual(gateway._provision_workdir(scope, "c1"), (repo, None))

    def test_a_repo_with_a_commit_gets_the_configured_number_of_slots(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            repo = make_repo(root)
            gateway, store, scope = self._gateway(root)
            scope.working_directory = repo
            self.assertEqual(gateway._slot_limit(scope), 3)
            scope.max_workers = 1
            self.assertEqual(gateway._slot_limit(scope), 1)

    def test_three_tasks_run_side_by_side_in_a_project_without_worktrees(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(Path(tmp))
            release = threading.Event()
            started: list[str] = []

            def fake_run(scope, conv_id, goal, schedule_id=None):
                started.append(goal)
                release.wait(timeout=10)
                return MagicMock(conversation_id=conv_id, status="succeeded", recommendation="ok")

            gateway._run_new_task = fake_run
            gateway._report_run_outcome = MagicMock()
            gateway._model_summary = MagicMock(return_value="m")
            self.addCleanup(release.set)
            for goal in ("a", "b", "c", "d"):
                gateway._start_or_queue_task(scope, goal, 12345)
            for _ in range(200):
                if len(started) >= 3:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(sorted(started), ["a", "b", "c"])            # the 4th waits, not the 2nd
            self.assertEqual(len(store.list_queued_tasks(scope.workspace_id)), 1)
            release.set()
            for _ in range(20):
                alive = [t for t in threading.enumerate() if t.name.startswith("agent-loop-")]
                if not alive:
                    break
                for t in alive:
                    t.join(timeout=5)

    def test_only_a_task_sharing_the_folder_gets_the_edit_lock(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(Path(tmp))
            shared = gateway._edit_lock_for(scope, "c1", None, store)
            self.assertIsNotNone(shared)
            acquire, release = shared
            store.acquire_task_slot(scope.workspace_id, "c1", max_slots=3)
            self.assertTrue(acquire())
            release()
            self.assertIsNone(store.get_edit_lock(scope.workspace_id))
            # Its own worktree: no lock. One worker: nothing to share: no lock.
            self.assertIsNone(gateway._edit_lock_for(scope, "c1", MagicMock(), store))
            scope.max_workers = 1
            self.assertIsNone(gateway._edit_lock_for(scope, "c1", None, store))

    def test_the_runner_is_given_the_edit_lock_when_the_folder_is_shared(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = self._gateway(Path(tmp))
            with patch("memtrace_harness.telegram_gateway.load_role_profiles", return_value={}), patch(
                "memtrace_harness.telegram_gateway.build_role_adapter_candidates", return_value={}
            ), patch("memtrace_harness.telegram_gateway.AgentLoopRunner") as runner:
                runner.return_value.run.return_value = MagicMock(
                    conversation_id="c1", status="succeeded", recommendation="ok"
                )
                gateway._run_new_task(scope, "c1", "改程式")
            self.assertIsNotNone(runner.call_args.kwargs["edit_lock"])


class EditLockStoreTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        for conv in ("a", "b"):
            self.store.acquire_task_slot("ws", conv, max_slots=3)

    def test_only_one_task_may_develop_at_a_time_and_it_can_retake_its_own_lock(self) -> None:
        self.assertTrue(self.store.acquire_edit_lock("ws", "a"))
        self.assertTrue(self.store.acquire_edit_lock("ws", "a"))
        self.assertFalse(self.store.acquire_edit_lock("ws", "b"))
        self.store.release_edit_lock("ws", "a")
        self.assertTrue(self.store.acquire_edit_lock("ws", "b"))

    def test_another_project_has_its_own_lock(self) -> None:
        self.store.acquire_task_slot("other", "x", max_slots=3)
        self.assertTrue(self.store.acquire_edit_lock("ws", "a"))
        self.assertTrue(self.store.acquire_edit_lock("other", "x"))

    def test_finishing_a_task_frees_its_edit_lock_with_its_slot(self) -> None:
        self.store.acquire_edit_lock("ws", "a")
        self.store.release_workspace_lock("ws", "a")
        self.assertTrue(self.store.acquire_edit_lock("ws", "b"))

    def test_a_lock_whose_owner_lost_its_slot_is_taken_over(self) -> None:
        self.store.acquire_edit_lock("ws", "a")
        with self.store._connection() as conn:       # the process died: slot row gone, lock row left
            conn.execute("DELETE FROM workspace_locks WHERE conversation_id = 'a'")
        self.assertTrue(self.store.acquire_edit_lock("ws", "b"))


class TaskStartedMessageTests(TestCase):
    def test_a_scheduled_start_names_the_schedule_and_what_it_does(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = _build_schedule_gateway(Path(tmp))
            gateway.send_message = MagicMock()
            gateway._launch_task = MagicMock()
            gateway._start_or_queue_task(scope, "每10分鐘檢查庫存報價", 12345, schedule_id="sched_75c5981703")
            text = gateway.send_message.call_args.args[1]
            self.assertIn("sched_75c5981703", text)
            self.assertIn("每10分鐘檢查庫存報價", text)
            self.assertNotIn("可能需要幾分鐘", text)

    def test_a_chat_task_start_says_what_started_without_the_boilerplate(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway, store, scope = _build_schedule_gateway(Path(tmp))
            gateway.send_message = MagicMock()
            gateway._launch_task = MagicMock()
            gateway._start_or_queue_task(scope, "修改移動停利公式", 12345)
            text = gateway.send_message.call_args.args[1]
            self.assertIn("修改移動停利公式", text)
            self.assertNotIn("可能需要幾分鐘", text)
            self.assertNotIn("sched_", text)
