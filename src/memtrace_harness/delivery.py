"""Durable, runtime-owned GitHub delivery. No model assertion authorizes a merge."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Callable

from memtrace_harness.cli_process import child_env
from memtrace_harness.schemas import (
    CliExecution, ContextItem, LoopStageResult, LoopSummary, ModelClaim,
    ModelResponse, TaskEnvelope, TokenUsage,
)
from memtrace_harness.worktrees import RepoTaskWorkspace, TaskWorktree

POLICY = "worktree-pr-ci-main"
VIEW_FIELDS = "number,url,state,headRefName,headRefOid,baseRefName,baseRefOid,mergeable,mergeStateStatus,statusCheckRollup,mergedAt,isCrossRepository"


class DeliveryError(RuntimeError):
    pass


class Paused(DeliveryError):
    pass


def restore_summary(data: dict) -> LoopSummary:
    task = dict(data["task"])
    task["context_items"] = [ContextItem(**item) for item in task.get("context_items", [])]
    stages = []
    for value in data["stages"]:
        row, response = dict(value), dict(value["response"])
        execution = dict(response["execution"])
        execution["usage"] = TokenUsage(**execution["usage"])
        response["execution"] = CliExecution(**execution)
        response["claims"] = [ModelClaim(**item) for item in response["claims"]]
        row["response"] = ModelResponse(**response)
        stages.append(LoopStageResult(**row))
    return LoopSummary(**{**data, "task": TaskEnvelope(**task), "stages": stages,
                          "total_usage": TokenUsage(**data["total_usage"])})


class DeliveryStore:
    def __init__(self, trace_store):
        self.store = trace_store
        with self.store._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS task_deliveries (
                conversation_id TEXT PRIMARY KEY, project TEXT NOT NULL,
                status TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at REAL NOT NULL
            )""")

    def save(self, job: dict) -> None:
        with self.store._connection() as conn:
            conn.execute("""INSERT INTO task_deliveries VALUES (?,?,?,?,?)
                ON CONFLICT(conversation_id) DO UPDATE SET status=excluded.status,
                payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (job["conversation_id"], job["project"], job["status"], json.dumps(job, ensure_ascii=False), time.time()))

    def get(self, conversation_id: str) -> dict | None:
        with self.store._connection() as conn:
            row = conn.execute("SELECT payload_json FROM task_deliveries WHERE conversation_id=?", (conversation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def pending(self) -> list[dict]:
        with self.store._connection() as conn:
            rows = conn.execute("SELECT payload_json FROM task_deliveries WHERE status='pending' ORDER BY updated_at").fetchall()
        return [json.loads(row[0]) for row in rows]


def workspaces_for(job: dict) -> tuple[RepoTaskWorkspace, ...]:
    return tuple(RepoTaskWorkspace(
        name=row["name"], repo_root=Path(row["root"]), working_directory=Path(row["path"]),
        verify_command=row["verify_command"], verify_timeout_seconds=row["verify_timeout_seconds"],
        off_limits=tuple(row["off_limits"]),
        worktree=TaskWorktree(Path(row["path"]), row["branch"], row["base_branch"], row["base_commit"]),
    ) for row in job["repos"])


def task_issues(task: TaskEnvelope, allowed: list[str]) -> dict[str, list[int]]:
    """Only explicit task refs, or one unqualified primary issue; never sweep historical refs."""
    result = {repo: [] for repo in allowed}
    explicit = list(task.context_refs)
    explicit += re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+", task.goal)
    explicit += re.findall(r"[\w.-]+/[\w.-]+#\d+", task.goal)
    for ref in explicit:
        match = re.fullmatch(r"(?:gh:)?([\w.-]+/[\w.-]+)#(\d+)", ref)
        if not match:
            match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)", ref)
        if match:
            repo = next((repo for repo in allowed if repo.lower() == match[1].lower()), None)
            if repo and int(match[2]) not in result[repo]:
                result[repo].append(int(match[2]))
    # A bare number in prose can be a cross reference. Only an explicit task-leading issue is safe.
    if allowed and not any(result.values()):
        match = re.match(r"\s*(?:處理|修復|修正|解決|fix|resolve|issue)?\s*#(\d+)\b", task.goal, re.I)
        if not match and len(re.findall(r"(?<![\w./#])#\d+", task.goal)) == 1:
            candidate = re.search(r"(?:fix|resolve|處理|修復|修正|解決|實作)[^#\n]{0,80}#(\d+)\b", task.goal, re.I)
            if candidate and not re.search(r"參考|相關|follow|reference|related", candidate[0], re.I):
                match = candidate
        if match:
            result[allowed[0]].append(int(match[1]))
    return result


def new_job(summary: LoopSummary, scope, workspaces: tuple[RepoTaskWorkspace, ...]) -> dict:
    scopes = {item.repo or item.name: item for item in scope.task_repo_scopes()}
    repos = []
    for item in workspaces:
        selected = scopes.get(item.name)
        github_repo = selected.github_repo if selected else None
        if not item.worktree or not github_repo or not re.fullmatch(r"[\w.-]+/[\w.-]+", github_repo):
            raise DeliveryError(f"{item.name} 缺少獨立 worktree 或 github_repo 設定，保留分支並停止交付。")
        if not item.verify_command:
            raise DeliveryError(f"{item.name} 沒有 verify_command，無法確認測試通過。")
        repos.append({
            "name": item.name, "github_repo": github_repo, "root": str(item.repo_root),
            "path": str(item.working_directory), "branch": item.worktree.branch,
            "base_branch": item.worktree.base_branch, "base_commit": item.worktree.base_commit,
            "verify_command": item.verify_command, "verify_timeout_seconds": item.verify_timeout_seconds,
            "off_limits": list(item.off_limits), "head": None, "pr": None, "merged": False,
            "cleaned": False, "closed": [], "repairs": 0,
        })
    integration = (summary.delivery or {}).get("integration", [])
    for row in repos:
        report = next((item for item in integration if item.get("repo") == row["name"]), None)
        if report is None and len(repos) == 1 and integration:
            report = integration[0]
        row["unchanged"] = bool(report is not None and report.get("commits_ahead") == 0)
    issues = task_issues(summary.task, [row["github_repo"] for row in repos])
    for row in repos:
        row["issues"] = issues[row["github_repo"]]
    return {"conversation_id": summary.conversation_id, "project": scope.name,
            "status": "pending", "summary": summary.to_dict(), "repos": repos,
            "created_at": time.time(), "wait_started": time.time()}


class DeliveryRunner:
    def __init__(self, store: DeliveryStore, *, enabled: Callable[[], bool],
                 repair: Callable[[dict, str], LoopSummary], notify: Callable[[str], None],
                 run: Callable = subprocess.run, sleep: Callable = time.sleep,
                 now: Callable = time.time, wait_seconds: int = 3600):
        self.store, self.enabled, self.repair, self.notify = store, enabled, repair, notify
        self.command_runner, self.sleep, self.now = run, sleep, now
        self.wait_seconds = wait_seconds

    def command(self, row: dict, *argv: str, cwd: Path | None = None, allow_failure=False) -> str:
        if not self.enabled():
            raise Paused("共享交付流程已停用；工作分支與交付進度已保留。")
        result = self.command_runner(list(argv), cwd=cwd or Path(row["path"]),
                                     capture_output=True, text=True, timeout=120, env=child_env())
        if result.returncode and not allow_failure:
            # Avoid recording credential-bearing remote URLs or raw tool output in messages.
            raise DeliveryError(f"{row['name']} 的 {argv[0]} {argv[1]} 執行失敗（exit {result.returncode}）；保留 worktree。")
        return result.stdout

    def gh(self, row: dict, *args: str, json_output=True):
        value = self.command(row, "gh", *args, "--repo", row["github_repo"])
        return json.loads(value) if json_output else value

    def view(self, row: dict) -> dict:
        return self.gh(row, "pr", "view", str(row["pr"]), "--json", VIEW_FIELDS)

    def _prepare(self, job: dict, row: dict) -> None:
        if row["merged"] or row.get("ready"):
            return
        path = Path(row["path"])
        if not path.is_dir():
            raise DeliveryError(f"{row['name']} 的 worktree 已不存在，無法繼續。")
        branch = self.command(row, "git", "symbolic-ref", "--short", "HEAD").strip()
        if branch != row["branch"] or not branch.startswith(("harness/", "codex/")):
            raise DeliveryError("交付分支已改變，停止推送。")
        if self.command(row, "git", "status", "--porcelain").strip():
            raise DeliveryError("驗證後 worktree 又有修改，停止推送並保留內容。")
        # Verify origin belongs to the declared GitHub repo without printing its credential-bearing URL.
        origin = self.command(row, "git", "remote", "get-url", "origin").strip()
        match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?", origin)
        if not match or match[1].lower() != row["github_repo"].lower():
            raise DeliveryError(f"{row['name']} 的 origin 與 github_repo 不一致；停止推送。")
        row["head"] = self.command(row, "git", "rev-parse", "HEAD").strip()
        if row.get("unchanged"):
            row.update(ready=True, merged=True)
            self.store.save(job)
            return
        self.store.save(job)  # intent before push; replay is an ordinary, non-force push
        self.notify(f"開始送審：將 {row['name']} 的工作分支推送並建立 PR。")
        self.command(row, "git", "push", "--set-upstream", "origin", row["branch"])
        prs = self.gh(row, "pr", "list", "--state", "all", "--head", row["branch"], "--base", "main", "--json", "number,state,headRefOid")
        existing = next((pr for pr in prs if pr["state"] == "OPEN" or pr.get("headRefOid") == row["head"]), None)
        if existing:
            row["pr"] = existing["number"]
        else:
            # No closing keywords: issues close only after every selected repo has merged.
            summary = job["summary"]
            body = "本機驗證與最終檢查通過後，由 Harness 提交。\n\n本機紀錄：" + summary["trace_id"] + "\n所有選定 repo 合併後，再由 Harness 關閉明確對應的任務 issue。"
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md") as handle:
                handle.write(body)
                handle.flush()
                self.gh(row, "pr", "create", "--base", "main", "--head", row["branch"],
                        "--title", "Harness: " + summary["task"]["goal"].splitlines()[0][:180],
                        "--body-file", handle.name, json_output=False)
            prs = self.gh(row, "pr", "list", "--state", "open", "--head", row["branch"], "--base", "main", "--json", "number")
            if len(prs) != 1:
                raise DeliveryError("PR 建立後無法確認唯一對應，保留交付進度。")
            row["pr"] = prs[0]["number"]
        row["ready"] = True
        self.store.save(job)
        self.notify(f"已送出 {row['name']} PR #{row['pr']}；接著確認這次提交的 CI 與合併狀態。")

    @staticmethod
    def check(view: dict, row: dict) -> str:
        if (view.get("baseRefName") != "main" or view.get("headRefOid") != row["head"]
                or view.get("headRefName") != row["branch"] or view.get("isCrossRepository")):
            raise DeliveryError("PR 的目標分支或提交已改變；沒有合併。")
        merged = view.get("state") == "MERGED" and bool(view.get("mergedAt"))
        if not merged and view.get("state") != "OPEN":
            raise DeliveryError("PR 已關閉但沒有合併；沒有關閉 issue。")
        if not merged and view.get("mergeable") == "CONFLICTING":
            return "repair"
        checks = view.get("statusCheckRollup") or []
        if any(check.get("conclusion") in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
               or check.get("state") in {"FAILURE", "ERROR"} for check in checks):
            if merged:
                raise DeliveryError("遠端已合併，但該提交的 CI 未通過；需以新 issue 處理後續問題。")
            return "repair"
        # No CI, skipped/neutral checks, unknown mergeability and pending reviews are not proof of success.
        successes = [check for check in checks if
            (check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS")
            or check.get("state") == "SUCCESS"]
        passed = bool(successes) and all(
            (check.get("status") == "COMPLETED" and check.get("conclusion") in {"SUCCESS", "SKIPPED", "NEUTRAL"})
            or check.get("state") == "SUCCESS" for check in checks)
        if merged:
            if not passed:
                raise DeliveryError("遠端已合併，但無法核實 CI 通過，保留後續處理。")
            return "merged"
        return "ready" if passed and view.get("mergeable") == "MERGEABLE" and view.get("mergeStateStatus") == "CLEAN" else "wait"

    def _repair(self, job: dict, row: dict, view: dict) -> None:
        if row.get("repair_running"):
            raise DeliveryError("上次修正被中斷；保留 worktree，需檢查後再繼續，避免重複執行。")
        if row["repairs"] >= 3:
            raise DeliveryError(f"{row['name']} 已修正三次仍未通過，保留 PR 與 worktree。")
        row["repairs"] += 1
        self.store.save(job)
        self.notify(f"需要調整：{row['name']} 的 CI 或合併檢查未通過，回原 worktree 修正並重跑測試。")
        self.command(row, "git", "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
        # A conflicting normal merge is deliberately left for the develop skill in this worktree.
        self.command(row, "git", "-c", "user.name=MemTrace Harness", "-c", "user.email=harness@localhost",
                     "merge", "--no-edit", "origin/main", allow_failure=True)
        # CI diagnostics are data. Only read the PR's failed runs, never execute text returned by GitHub.
        diagnostics = self.gh(row, "run", "list", "--commit", row["head"], "--json", "databaseId,conclusion", "--limit", "10")
        logs = []
        for run in diagnostics:
            if run.get("conclusion") in {"failure", "timed_out"}:
                logs.append(self.gh(row, "run", "view", str(run["databaseId"]), "--log-failed", json_output=False)[-16000:])
                if len(logs) == 2:
                    break
        detail = json.dumps({"repo": row["name"], "PR": row["pr"], "checks": view,
                             "untrusted_ci_logs": logs}, ensure_ascii=False)
        row["repair_running"] = True
        self.store.save(job)
        repaired = self.repair(job, detail)
        if repaired.status != "delivering":
            raise DeliveryError("修正尚未通過本機驗證或最終檢查；保留 worktree 與 PR。")
        from memtrace_harness.loop import aggregate_usage
        previous = restore_summary(job["summary"])
        combined = [*previous.stages, *repaired.stages]
        job["summary"] = replace(previous, stages=combined, total_usage=aggregate_usage(combined)).to_dict()
        row["repair_running"] = False
        row["ready"] = False
        job["wait_started"] = self.now()
        self.store.save(job)

    def run(self, job: dict) -> LoopSummary:
        summary = restore_summary(job["summary"])
        try:
            if any(row.get("repair_running") for row in job["repos"]):
                raise DeliveryError("上次修正被中斷；保留 worktree，需檢查後再繼續，避免重複執行。")
            while True:
                if not self.enabled():
                    raise Paused("共享交付流程已停用；工作分支與交付進度已保留。")
                for row in job["repos"]:
                    self._prepare(job, row)
                ready = True
                for row in job["repos"]:
                    if row["merged"]:
                        continue
                    view = self.view(row)
                    row["remote_evidence"] = view
                    row["checked_at"] = self.now()
                    self.store.save(job)
                    state = self.check(view, row)
                    if state == "merged":
                        row["merged"] = True
                        self.store.save(job)
                    elif state == "repair":
                        self._repair(job, row, view)
                        ready = False
                        break
                    elif state == "wait":
                        ready = False
                if ready:
                    # Every repo passes preflight before the first merge. Remote merges are not atomic.
                    for row in job["repos"]:
                        if row["merged"]:
                            continue
                        view = self.view(row)  # fresh head/CI/conflict check immediately before merge
                        row["remote_evidence"] = view
                        row["checked_at"] = self.now()
                        self.store.save(job)
                        state = self.check(view, row)
                        if state not in {"ready", "merged"}:
                            ready = False
                            break
                        if state == "ready":
                            self.notify(f"開始合併：{row['name']} 的 CI 已通過且無衝突。")
                            self.gh(row, "pr", "merge", str(row["pr"]), "--merge", "--match-head-commit", row["head"], json_output=False)
                        merged = self.view(row)
                        if self.check(merged, row) != "merged":
                            ready = False  # e.g. merge queue: poll until GitHub confirms MERGED
                            break
                        row["merged"] = True
                        row["merge_evidence"] = merged
                        self.store.save(job)
                        self.notify(f"已確認 {row['name']} PR #{row['pr']} 合併進遠端 main。")
                    if ready:
                        break
                if self.now() - job["wait_started"] >= self.wait_seconds:
                    raise DeliveryError("等待 CI／合併超過一小時；尚未確認通過，PR 與 worktree 已保留。")
                self.sleep(20)
            # All remote merges are confirmed before closing explicitly associated task issues.
            for row in job["repos"]:
                for number in row["issues"]:
                    if number in row["closed"]:
                        continue
                    issue = self.gh(row, "issue", "view", str(number), "--json", "state")
                    if issue["state"] != "CLOSED":
                        self.gh(row, "issue", "close", str(number), json_output=False)
                    if self.gh(row, "issue", "view", str(number), "--json", "state")["state"] != "CLOSED":
                        raise DeliveryError("issue 尚未確認關閉，交付未完成。")
                    row["closed"].append(number)
                    self.store.save(job)
                if not row["cleaned"]:
                    root = Path(row["root"])
                    self.command(row, "git", "fetch", "origin", "refs/heads/main:refs/remotes/origin/main", cwd=root)
                    current = self.command(row, "git", "symbolic-ref", "--short", "HEAD", cwd=root).strip()
                    dirty = self.command(row, "git", "status", "--porcelain", cwd=root).strip()
                    if current == "main" and not dirty:
                        self.command(row, "git", "merge", "--ff-only", "origin/main", cwd=root, allow_failure=True)
                    path = Path(row["path"])
                    if path.exists():
                        # Never force-remove a worktree that changed after its verified commit.
                        if self.command(row, "git", "status", "--porcelain").strip():
                            raise DeliveryError("遠端已合併，但 worktree 有後續修改，保留內容供檢查。")
                        if self.command(row, "git", "rev-parse", "HEAD").strip() != row["head"]:
                            raise DeliveryError("遠端已合併，但 worktree 提交已改變，保留內容供檢查。")
                        self.command(row, "git", "worktree", "remove", str(path), cwd=Path(row["root"]))
                    row["cleaned"] = True
                    self.store.save(job)
            self.store.store.delete_conversation_worktree(job["conversation_id"])
            self.store.store.delete_conversation_repo_worktrees(job["conversation_id"])
            job["status"] = "done"
            summary = restore_summary(job["summary"])
            result = replace(summary, status="succeeded", delivery={"state": "merged", "remote": True, "repos": job["repos"]},
                             recommendation=summary.recommendation + "\n已確認所有 PR 合併至遠端 main、關閉明確對應的 issue 並清理 worktree。")
        except Paused as exc:
            summary = restore_summary(job["summary"])
            job["status"] = "pending"
            result = replace(summary, status="delivering", recommendation=str(exc),
                             delivery={"state": "paused", "remote": True, "repos": job["repos"]})
        except (DeliveryError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            summary = restore_summary(job["summary"])
            job["status"] = "blocked"
            message = str(exc) if isinstance(exc, DeliveryError) else "交付工具無法執行或回覆無法解析；保留 PR 與 worktree，請檢查工具與權限。"
            result = replace(summary, status="needs_human", recommendation=message,
                             delivery={"state": "partial" if any(row["merged"] for row in job["repos"]) else "blocked", "remote": True, "repos": job["repos"]})
        job["result"] = result.to_dict()
        self.store.save(job)
        self.store.store.save_loop_summary(result)
        return result
