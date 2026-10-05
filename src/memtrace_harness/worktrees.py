from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import subprocess
import threading

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 120

# Merges into one project's checkout must not interleave: two tasks finishing together
# would otherwise race on the repo's index.
_merge_locks: dict[str, threading.Lock] = {}
_merge_locks_guard = threading.Lock()


def _merge_lock_for(repo: Path) -> threading.Lock:
    key = str(repo.resolve())
    with _merge_locks_guard:
        return _merge_locks.setdefault(key, threading.Lock())


@dataclass(frozen=True)
class TaskWorktree:
    path: Path
    branch: str
    base_branch: str | None
    base_commit: str | None


@dataclass(frozen=True)
class IntegrationReport:
    """What a finished task would bring back into the project's main checkout — shown
    to Controller so it can decide to merge, and re-computed when it does."""

    branch: str
    base_branch: str | None
    commits_ahead: int
    files_changed: list[str]
    base_moved: bool
    main_checkout_clean: bool
    # True / False when git could dry-run the merge, None when it could not tell.
    merges_cleanly: bool | None
    conflicts: list[str]

    @property
    def has_changes(self) -> bool:
        return self.commits_ahead > 0

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "base_branch": self.base_branch,
            "commits_ahead": self.commits_ahead,
            "files_changed": self.files_changed,
            "base_moved_since_task_started": self.base_moved,
            "main_checkout_clean": self.main_checkout_clean,
            "merges_cleanly": self.merges_cleanly,
            "conflicts": self.conflicts,
        }


@dataclass(frozen=True)
class MergeResult:
    ok: bool
    detail: str
    conflicts: list[str]


def _git(cwd: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def is_git_repo(path: Path) -> bool:
    try:
        result = _git(path, "rev-parse", "--is-inside-work-tree", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_commits(path: Path) -> bool:
    """False for a freshly `git init`-ed repo whose HEAD is still unborn: there is no
    commit to branch a worktree from."""
    try:
        return _git(path, "rev-parse", "--verify", "--quiet", "HEAD", timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def can_use_worktrees(path: Path) -> bool:
    return is_git_repo(path) and has_commits(path)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "project"


class WorktreeManager:
    """Gives each concurrent task its own `git worktree` on its own branch, so several
    Developer runs on one project never edit the same files, and brings a finished
    task's branch back into the project's main checkout."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, repo: Path, project: str, conversation_id: str) -> TaskWorktree:
        path = (self.root / _safe_name(project) / conversation_id).resolve()
        branch = f"harness/{conversation_id}"
        base_branch = self.current_branch(repo)
        head = _git(repo, "rev-parse", "HEAD")
        if head.returncode != 0:
            raise RuntimeError(f"cannot read HEAD of {repo}: {head.stderr.strip()}")
        base_commit = head.stdout.strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        added = _git(repo, "worktree", "add", "-b", branch, str(path), base_commit)
        if added.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {added.stderr.strip()}")
        return TaskWorktree(path=path, branch=branch, base_branch=base_branch, base_commit=base_commit)

    def run_setup_command(self, worktree: TaskWorktree, repo: Path, command: str, timeout: int) -> None:
        """A project's own hook to make a fresh checkout usable (link node_modules,
        copy an .env — whatever git does not carry). HARNESS_REPO_ROOT points at the
        main checkout."""
        env = {**os.environ, "HARNESS_REPO_ROOT": str(repo)}
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            cwd=worktree.path, capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"worktree setup command failed ({result.returncode}): "
                f"{(result.stderr or result.stdout).strip()[-1000:]}"
            )

    @staticmethod
    def current_branch(repo: Path) -> str | None:
        result = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=10)
        name = result.stdout.strip()
        return name if result.returncode == 0 and name else None

    def commit_all(self, worktree: TaskWorktree, message: str) -> bool:
        """Commit whatever the task left in its worktree. True if a commit was made."""
        status = _git(worktree.path, "status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or not status.stdout.strip():
            return False
        _git(worktree.path, "add", "-A")
        commit = _git(
            worktree.path,
            "-c", "user.name=MemTrace Harness",
            "-c", "user.email=harness@localhost",
            "commit", "-m", message,
        )
        if commit.returncode != 0:
            logger.warning(f"commit in {worktree.path} failed: {commit.stderr.strip()}")
            return False
        return True

    def commits_ahead(self, repo: Path, worktree: TaskWorktree) -> int:
        base = worktree.base_commit or "HEAD"
        result = _git(repo, "rev-list", "--count", f"{base}..{worktree.branch}", timeout=30)
        try:
            return int(result.stdout.strip()) if result.returncode == 0 else 0
        except ValueError:
            return 0

    def has_uncommitted(self, worktree: TaskWorktree) -> bool:
        status = _git(worktree.path, "status", "--porcelain", "--untracked-files=all")
        return status.returncode == 0 and bool(status.stdout.strip())

    def integration_report(self, repo: Path, worktree: TaskWorktree) -> IntegrationReport:
        base = worktree.base_commit or "HEAD"
        files = _git(repo, "diff", "--name-only", f"{base}...{worktree.branch}", timeout=30)
        changed = [line for line in files.stdout.splitlines() if line.strip()][:200]
        target = worktree.base_branch or "HEAD"
        head = _git(repo, "rev-parse", target, timeout=10)
        base_moved = bool(
            worktree.base_commit and head.returncode == 0
            and head.stdout.strip() != worktree.base_commit
        )
        main_clean = self._main_checkout_ready(repo, worktree)[0]
        merges_cleanly, conflicts = self._dry_run_merge(repo, worktree, target)
        return IntegrationReport(
            branch=worktree.branch,
            base_branch=worktree.base_branch,
            commits_ahead=self.commits_ahead(repo, worktree),
            files_changed=changed,
            base_moved=base_moved,
            main_checkout_clean=main_clean,
            merges_cleanly=merges_cleanly,
            conflicts=conflicts,
        )

    @staticmethod
    def _dry_run_merge(
        repo: Path, worktree: TaskWorktree, target: str
    ) -> tuple[bool | None, list[str]]:
        # `git merge-tree --write-tree` (git >= 2.38) merges in memory: no checkout,
        # no index, nothing to clean up.
        result = _git(repo, "merge-tree", "--write-tree", "--name-only", target, worktree.branch, timeout=60)
        if result.returncode == 0:
            return True, []
        if result.returncode == 1:
            # Output: the merged tree's oid, then one conflicted path per line, then a
            # blank line and informational messages.
            paths = [ln for ln in result.stdout.split("\n\n", 1)[0].splitlines()[1:] if ln.strip()]
            return False, paths[:50]
        return None, []

    def _main_checkout_ready(self, repo: Path, worktree: TaskWorktree) -> tuple[bool, str]:
        if worktree.base_branch is None:
            return False, "the project checkout was on a detached HEAD when the task started"
        if self.current_branch(repo) != worktree.base_branch:
            return False, (
                f"the project checkout is no longer on '{worktree.base_branch}' "
                f"(now on '{self.current_branch(repo)}')"
            )
        status = _git(repo, "status", "--porcelain", "--untracked-files=no")
        if status.returncode != 0 or status.stdout.strip():
            return False, "the project checkout has uncommitted changes"
        return True, ""

    def merge(self, repo: Path, worktree: TaskWorktree, message: str) -> MergeResult:
        """Merge the task branch into the project's main checkout. Refuses (rather
        than guessing) when that checkout is dirty or on another branch, and aborts
        cleanly on conflict so the checkout is never left half-merged."""
        with _merge_lock_for(repo):
            ready, why_not = self._main_checkout_ready(repo, worktree)
            if not ready:
                return MergeResult(False, f"Not merged: {why_not}.", [])
            result = _git(
                repo,
                "-c", "user.name=MemTrace Harness",
                "-c", "user.email=harness@localhost",
                "merge", "--no-ff", "-m", message, worktree.branch,
            )
            if result.returncode == 0:
                return MergeResult(True, f"Merged '{worktree.branch}' into '{worktree.base_branch}'.", [])
            conflicts = [
                ln for ln in _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()
                if ln.strip()
            ]
            _git(repo, "merge", "--abort")
            return MergeResult(
                False,
                f"Merge of '{worktree.branch}' into '{worktree.base_branch}' failed and was "
                f"aborted: {(result.stdout + result.stderr).strip()[-600:]}",
                conflicts,
            )

    def remove(self, repo: Path, worktree: TaskWorktree, *, delete_branch: bool = True) -> None:
        removed = _git(repo, "worktree", "remove", "--force", str(worktree.path))
        if removed.returncode != 0:
            logger.warning(f"git worktree remove {worktree.path} failed: {removed.stderr.strip()}")
        if delete_branch:
            # -d, not -D: a branch holding commits that were never merged is kept.
            _git(repo, "branch", "-d", worktree.branch)
