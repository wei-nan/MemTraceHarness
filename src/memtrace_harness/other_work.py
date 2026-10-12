"""What else is going on in a project, so a new task does not redo or collide with it (operator request 2026-10-12).

Why this exists: on 2026-10-12 three tasks on Beri#194 each started from their own context. One task's Developer CLI was
still working when a second task started; a third started while a fourth was in the middle of a 49-minute Developer
stage. The result was two pull requests for one issue and two overlapping fixes stacked on one branch, and no stage
ever said so in a way that stopped it.

``collect_other_work`` lists the project's unfinished work from facts the Harness already holds: tasks running now,
tasks waiting in the queue, and tasks whose own worktree holds commits or edits that are not merged. It looks at the
worktree itself (``HEAD`` and ``git status``), not at the branch name the Harness recorded, because a CLI may rename or
switch the branch it works on. ``overlapping`` is the deterministic part: it compares issue numbers (``#194``,
``issue-194``, ``gh:owner/repo#194``, ``.../issues/194``) and nothing else. A goal without a number is never matched here; it only gets
the list as context.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

WINDOW = timedelta(days=7)
MAX_ROWS = 40
MAX_LISTED = 12
GIT_SECONDS = 10

_ISSUE = re.compile(r"#(\d{1,6})\b|\bissue[-_ ]?(\d{1,6})\b|/(?:issues|pull)/(\d{1,6})\b", re.IGNORECASE)

RUNNING, QUEUED, PAUSED, STOPPED, UNMERGED = "running", "queued", "paused", "stopped", "unmerged"
_KIND_LABEL = {
    RUNNING: "執行中", QUEUED: "排隊中", PAUSED: "停在等人（有未合併的變更）",
    STOPPED: "已中斷或失敗（有未合併的變更）", UNMERGED: "已完成但變更尚未合併",
}


def issue_numbers(*texts: str) -> set[int]:
    found: set[int] = set()
    for text in texts:
        for match in _ISSUE.finditer(text or ""):
            found.add(int(match.group(1) or match.group(2) or match.group(3)))
    return found


@dataclass(frozen=True)
class OtherWork:
    conversation_id: str
    kind: str
    goal: str
    branch: str | None
    ahead: int
    dirty: bool
    issues: frozenset[int]
    updated_at: str


def _git(path: str, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_SECONDS, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _worktree_state(row: dict[str, Any], git: Callable[..., str | None]) -> tuple[str | None, int, bool, str] | None:
    """(actual branch, commits not yet in the base branch, uncommitted edits, commit subjects), or None when the
    worktree is gone or cannot be read."""
    path = row.get("path")
    if not path or not Path(path).is_dir():
        return None
    # Measured against the base branch as it is now, so work a converge stage already merged is not "unmerged";
    # the start commit is only the fallback when the base branch is not recorded.
    base = row.get("base_branch") or row.get("base_commit")
    head = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if head is None or not base:
        return None
    count = git(path, "rev-list", "--count", f"{base}..HEAD")
    status = git(path, "status", "--porcelain")
    subjects = git(path, "log", "--format=%s", "-n", "10", f"{base}..HEAD") or ""
    return (None if head == "HEAD" else head, int(count) if (count or "").isdigit() else 0, bool(status), subjects)


def collect_other_work(
    store: Any,
    workspace_id: str,
    *,
    exclude: str | None = None,
    live: dict[str, bool] | None = None,
    now: datetime | None = None,
    git: Callable[..., str | None] = _git,
) -> list[OtherWork]:
    """`live` maps the conversation ids with a thread running in this process to whether that run is a schedule's."""
    live = live or {}
    now = now or datetime.now(timezone.utc)
    items: list[OtherWork] = []
    rows = store.list_conversations_for_overview(workspace_id, (now - WINDOW).isoformat(), MAX_ROWS)
    for row in rows:
        conv_id = row["id"]
        if conv_id == exclude or live.get(conv_id) is True:
            continue
        goal = row.get("goal") or ""
        state = _worktree_state(row, git)
        branch, ahead, dirty, subjects = state if state else (row.get("branch"), 0, False, "")
        if conv_id in live:
            kind = RUNNING
        elif state is None or (ahead == 0 and not dirty):
            continue
        elif row["state"] == "needs_human":
            kind = PAUSED
        elif row["state"] == "succeeded":
            kind = UNMERGED
        else:
            kind = STOPPED
        items.append(
            OtherWork(
                conversation_id=conv_id, kind=kind, goal=goal, branch=branch, ahead=ahead, dirty=dirty,
                issues=frozenset(issue_numbers(goal, branch or "", subjects)), updated_at=row["updated_at"],
            )
        )
    for queued in store.list_queued_tasks(workspace_id):
        payload = queued.get("payload") or {}
        goal = str(payload.get("goal") or "")
        if queued.get("schedule_id") or not goal:
            continue
        items.append(
            OtherWork(
                conversation_id=f"queue_{queued['id']}", kind=QUEUED, goal=goal, branch=None, ahead=0, dirty=False,
                issues=frozenset(issue_numbers(goal)), updated_at=str(queued.get("created_at") or ""),
            )
        )
    return items


def overlapping(items: list[OtherWork], goal: str) -> list[OtherWork]:
    wanted = issue_numbers(goal)
    return [item for item in items if wanted and item.issues & wanted]


def describe(item: OtherWork) -> str:
    bits = [f"[{_KIND_LABEL[item.kind]}] {item.conversation_id}"]
    if item.branch:
        bits.append(f"分支 {item.branch}")
    if item.ahead:
        bits.append(f"有 {item.ahead} 個 commit 還不在主線")
    if item.dirty:
        bits.append("工作目錄有未提交的修改")
    if item.issues:
        bits.append("涉及 " + "、".join(f"#{n}" for n in sorted(item.issues)))
    return "，".join(bits) + f"。目標：{' '.join(item.goal.split())[:160]}"


def render_for_task(items: list[OtherWork]) -> str:
    shown = sorted(items, key=lambda i: i.updated_at, reverse=True)[:MAX_LISTED]
    return (
        "同一個專案裡還有下面這些尚未收尾的任務，它們不是你的工作。開始前先看清楚：如果其中有任務處理同一個 issue 或同一批檔案，"
        "不要另起一套重複的修改——先檢視它的分支現況（git log、git diff），在結果裡說明你接續或避開了什麼；"
        "不確定時問人，不要硬做。\n- " + "\n- ".join(describe(i) for i in shown)
    )
