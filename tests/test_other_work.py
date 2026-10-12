from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness import other_work
from memtrace_harness.other_work import OtherWork
from tests.test_task_abandon_and_heartbeat import _service


def _sh(cwd: Path, *argv: str) -> None:
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True,
                   env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd)})


class FakeStore:
    def __init__(self, rows, queued=()):
        self.rows, self.queued = rows, list(queued)

    def list_conversations_for_overview(self, workspace_id, since, limit):
        return self.rows

    def list_queued_tasks(self, workspace_id):
        return self.queued


def _row(conv_id, state, path=None, branch=None, base_commit=None, goal="", updated="2026-10-12T10:00:00+00:00"):
    return {"id": conv_id, "state": state, "updated_at": updated, "goal": goal, "path": path, "branch": branch,
            "base_branch": "main", "base_commit": base_commit}


class IssueNumberTests(TestCase):
    def test_numbers_come_from_hash_issue_dash_and_gh_refs(self) -> None:
        self.assertEqual(other_work.issue_numbers("修 #194 與 gh:wei-nan/Beri#193", "feat/issue-195-x"), {193, 194, 195})
        self.assertEqual(other_work.issue_numbers("沒有編號", "build 12345"), set())


class CollectTests(TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        _sh(self.tmp, "git", "init", "-q", "-b", "main")
        (self.tmp / "a.txt").write_text("1")
        _sh(self.tmp, "git", "add", ".")
        _sh(self.tmp, "git", "commit", "-q", "-m", "base")
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.tmp, capture_output=True, text=True).stdout.strip()

    def _worktree(self, name: str, *, commit: str | None = None, dirty: bool = False) -> Path:
        path = self.tmp / "wt" / name
        _sh(self.tmp, "git", "worktree", "add", "-q", "-b", f"harness/{name}", str(path))
        if commit:
            _sh(path, "git", "checkout", "-q", "-b", "feat/issue-194-renamed")  # the CLI switched branches
            (path / "b.txt").write_text("2")
            _sh(path, "git", "add", ".")
            _sh(path, "git", "commit", "-q", "-m", commit)
        if dirty:
            (path / "c.txt").write_text("3")
        return path

    def test_a_worktree_with_unmerged_commits_is_listed_under_the_branch_it_really_has(self) -> None:
        path = self._worktree("chat_a", commit="feat: do it (#194)")
        store = FakeStore([_row("chat_a", "failed", str(path), "harness/chat_a", self.base, goal="整合逾時分支")])
        (item,) = other_work.collect_other_work(store, "ws", live={})
        self.assertEqual((item.kind, item.branch, item.ahead), (other_work.STOPPED, "feat/issue-194-renamed", 1))
        self.assertEqual(item.issues, frozenset({194}))  # from the branch name and the commit subject

    def test_clean_untouched_worktrees_this_task_itself_and_schedule_runs_are_left_out(self) -> None:
        clean = self._worktree("chat_clean")
        edited = self._worktree("chat_edit", dirty=True)
        rows = [
            _row("chat_clean", "succeeded", str(clean), "harness/chat_clean", self.base),
            _row("chat_edit", "needs_human", str(edited), "harness/chat_edit", self.base, goal="g"),
            _row("chat_sched", "active", goal="每 5 分鐘檢查"),
            _row("chat_me", "active", goal="mine"),
        ]
        items = other_work.collect_other_work(FakeStore(rows), "ws", exclude="chat_me", live={"chat_sched": True, "chat_me": False})
        self.assertEqual([(i.conversation_id, i.kind, i.dirty) for i in items], [("chat_edit", other_work.PAUSED, True)])

    def test_work_already_merged_into_the_base_branch_is_not_listed(self) -> None:
        path = self._worktree("chat_m", commit="feat: x (#194)")
        _sh(self.tmp, "git", "merge", "-q", "--no-edit", "feat/issue-194-renamed")
        store = FakeStore([_row("chat_m", "succeeded", str(path), "harness/chat_m", self.base)])
        self.assertEqual(other_work.collect_other_work(store, "ws", live={}), [])

    def test_a_running_task_and_a_queued_task_are_listed(self) -> None:
        store = FakeStore(
            [_row("chat_run", "active", goal="修 #194")],
            queued=[{"id": 7, "payload": {"goal": "做 #195"}, "schedule_id": None, "created_at": "t"},
                    {"id": 8, "payload": {"goal": "排程"}, "schedule_id": "sched_1", "created_at": "t"}],
        )
        items = other_work.collect_other_work(store, "ws", live={"chat_run": False})
        self.assertEqual([(i.conversation_id, i.kind) for i in items], [("chat_run", other_work.RUNNING), ("queue_7", other_work.QUEUED)])

    def test_overlap_is_by_issue_number_only(self) -> None:
        items = [OtherWork("a", other_work.RUNNING, "g", None, 0, False, frozenset({194}), "t"),
                 OtherWork("b", other_work.RUNNING, "g", None, 0, False, frozenset(), "t")]
        self.assertEqual([i.conversation_id for i in other_work.overlapping(items, "接續 Issue #194")], ["a"])
        self.assertEqual(other_work.overlapping(items, "修 CI 失敗"), [])


class ServiceWiringTests(TestCase):
    def setUp(self) -> None:
        self.service = _service(Path(tempfile.mkdtemp()))
        self.item = OtherWork("chat_old", other_work.UNMERGED, "g", "feat/x", 2, False, frozenset({194}), "2026-10-12T10:00:00+00:00")

    def _scope(self):
        return self.service.projects[0]

    def test_a_goal_on_a_covered_issue_is_held_with_a_confirm_card(self) -> None:
        scope = self._scope()
        sent = []
        with patch.object(self.service, "_other_work", return_value=[self.item]), patch.object(
            self.service, "send_message_with_keyboard", side_effect=lambda a, t, k, **kw: sent.append((t, k))
        ):
            self.assertTrue(self.service._hold_for_overlap(scope, "接續 #194", None))
            self.assertFalse(self.service._hold_for_overlap(scope, "修 CI 失敗", None))
        self.assertEqual(len(sent), 1)
        self.assertIn("chat_old", sent[0][0])
        ids = [b["id"] for b in sent[0][1][0]]
        self.assertEqual([i.split(":")[0] for i in ids], ["dup_start", "dup_cancel"])

    def test_start_runs_the_held_task_once_and_cancel_does_not(self) -> None:
        scope = self._scope()
        with patch.object(self.service, "_other_work", return_value=[self.item]), patch.object(
            self.service, "send_message_with_keyboard"
        ):
            self.service._hold_for_overlap(scope, "接續 #194", None)
            self.service._hold_for_overlap(scope, "再一個 #194", None)
        first, second = list(self.service._pending_overlap)
        with patch.object(self.service, "_start_or_queue_task") as start:
            self.service._resolve_overlap("dup_start", first, None, None)
            self.service._resolve_overlap("dup_start", first, None, None)  # a double tap
            self.service._resolve_overlap("dup_cancel", second, None, None)
        start.assert_called_once_with(scope, "接續 #194", None)

    def test_other_work_reaches_the_task_as_a_context_item(self) -> None:
        scope = self._scope()
        with patch.object(self.service, "_other_work", return_value=[self.item]):
            items = self.service._project_context_items(scope, exclude_conversation="chat_me")
        (item,) = [i for i in items if i.ref.startswith("harness:other-work:")]
        self.assertEqual(item.content_type, "context")  # the Controller only reads this content type
        self.assertIn("chat_old", item.body)
        with patch.object(self.service, "_other_work", return_value=[]):
            self.assertFalse([i for i in self.service._project_context_items(scope) if "other-work" in i.ref])
