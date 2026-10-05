from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.worktrees import WorktreeManager, is_git_repo


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


class WorktreeManagerTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = make_repo(self.root)
        self.manager = WorktreeManager(self.root / "worktrees")

    def test_each_task_gets_its_own_checkout_on_its_own_branch(self) -> None:
        first = self.manager.create(self.repo, "proj", "chat_1")
        second = self.manager.create(self.repo, "proj", "chat_2")

        self.assertNotEqual(first.path, second.path)
        self.assertEqual((first.branch, first.base_branch), ("harness/chat_1", "main"))
        (first.path / "a.txt").write_text("changed in first\n")
        self.assertEqual((second.path / "a.txt").read_text(), "one\n")
        self.assertEqual((self.repo / "a.txt").read_text(), "one\n")

    def test_is_git_repo(self) -> None:
        self.assertTrue(is_git_repo(self.repo))
        plain = self.root / "plain"
        plain.mkdir()
        self.assertFalse(is_git_repo(plain))

    def test_merge_brings_the_task_branch_back_and_cleanup_removes_it(self) -> None:
        wt = self.manager.create(self.repo, "proj", "chat_1")
        (wt.path / "new.txt").write_text("hello\n")
        self.assertTrue(self.manager.commit_all(wt, "add new"))

        report = self.manager.integration_report(self.repo, wt)
        self.assertEqual((report.commits_ahead, report.files_changed), (1, ["new.txt"]))
        self.assertTrue(report.merges_cleanly)
        self.assertTrue(report.main_checkout_clean)

        result = self.manager.merge(self.repo, wt, "merge chat_1")
        self.assertTrue(result.ok, result.detail)
        self.assertEqual((self.repo / "new.txt").read_text(), "hello\n")

        self.manager.remove(self.repo, wt)
        self.assertFalse(wt.path.exists())
        self.assertNotIn("harness/chat_1", git(self.repo, "branch", "--list"))

    def test_commit_all_reports_when_nothing_changed(self) -> None:
        wt = self.manager.create(self.repo, "proj", "chat_1")
        self.assertFalse(self.manager.commit_all(wt, "nothing"))
        self.assertEqual(self.manager.commits_ahead(self.repo, wt), 0)

    def test_conflicting_branches_are_detected_and_the_merge_aborts_cleanly(self) -> None:
        first = self.manager.create(self.repo, "proj", "chat_1")
        second = self.manager.create(self.repo, "proj", "chat_2")
        (first.path / "a.txt").write_text("from first\n")
        (second.path / "a.txt").write_text("from second\n")
        self.manager.commit_all(first, "first")
        self.manager.commit_all(second, "second")

        self.assertTrue(self.manager.merge(self.repo, first, "merge 1").ok)

        report = self.manager.integration_report(self.repo, second)
        self.assertTrue(report.base_moved)
        self.assertFalse(report.merges_cleanly)
        self.assertEqual(report.conflicts, ["a.txt"])

        result = self.manager.merge(self.repo, second, "merge 2")
        self.assertFalse(result.ok)
        self.assertEqual(result.conflicts, ["a.txt"])
        # Aborted: the main checkout is back to the first merge, not left mid-merge.
        self.assertEqual((self.repo / "a.txt").read_text(), "from first\n")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        # The conflicting work survives on its branch, so removal keeps it.
        self.manager.remove(self.repo, second)
        self.assertIn("harness/chat_2", git(self.repo, "branch", "--list"))

    def test_merge_refuses_a_dirty_main_checkout(self) -> None:
        wt = self.manager.create(self.repo, "proj", "chat_1")
        (wt.path / "new.txt").write_text("x\n")
        self.manager.commit_all(wt, "add")
        (self.repo / "a.txt").write_text("someone is editing this\n")

        result = self.manager.merge(self.repo, wt, "merge")

        self.assertFalse(result.ok)
        self.assertIn("uncommitted", result.detail)
        self.assertFalse((self.repo / "new.txt").exists())

    def test_merge_refuses_when_the_checkout_changed_branch(self) -> None:
        wt = self.manager.create(self.repo, "proj", "chat_1")
        (wt.path / "new.txt").write_text("x\n")
        self.manager.commit_all(wt, "add")
        git(self.repo, "checkout", "-b", "other")

        result = self.manager.merge(self.repo, wt, "merge")

        self.assertFalse(result.ok)
        self.assertIn("no longer on 'main'", result.detail)

    def test_setup_command_runs_in_the_worktree_with_the_repo_root(self) -> None:
        wt = self.manager.create(self.repo, "proj", "chat_1")
        self.manager.run_setup_command(wt, self.repo, 'echo "$HARNESS_REPO_ROOT" > root.txt', 10)
        self.assertEqual((wt.path / "root.txt").read_text().strip(), str(self.repo))
        with self.assertRaises(RuntimeError):
            self.manager.run_setup_command(wt, self.repo, "exit 3", 10)
