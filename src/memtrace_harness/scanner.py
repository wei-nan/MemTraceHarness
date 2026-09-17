from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalManager
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.telegram_gateway import TelegramGateway
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)


def _parse_task_body(body: Any) -> Any:
    """Task Node bodies are JSON (sometimes fenced in a ```json code block when
    content_format is markdown); returns None for anything else instead of
    raising, so a malformed node just fails the readiness check rather than
    crashing the whole scan pass."""
    if not isinstance(body, str):
        return None
    text = body.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.strip()
        if text.endswith("```"):
            text = text[: -3].strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_ready_task_node(node: dict[str, Any]) -> bool:
    """Task Node Schema judgment (ws_bff64012/mem_353f7454, adopted for harness
    pickup in ws_79bfe681/mem_99f7edc7)."""
    tags = node.get("tags")
    if not isinstance(tags, list) or "task" not in tags or "status:open" not in tags:
        return False
    body = _parse_task_body(node.get("body"))
    if not isinstance(body, dict):
        return False
    if body.get("checkpoint") != "build" or body.get("current_stage") != "dev":
        return False
    gate_state = body.get("gate_state")
    return isinstance(gate_state, dict) and gate_state.get("latest_verdict") == "PASS"


class UnattendedScanner:
    def __init__(
        self,
        config: HarnessConfig,
        trace_store: TraceStore,
        projects: list[ProjectScope],
        memtrace_client: MemTraceClient | None = None,
        gateway: TelegramGateway | dict[str, TelegramGateway] | None = None,
        approval_mgr: ApprovalManager | None = None,
    ) -> None:
        self.config = config
        self.trace_store = trace_store
        self.projects = projects
        self.memtrace_client = memtrace_client
        self.gateway = gateway
        self.approval_mgr = approval_mgr

    def _gateway_for(self, scope: ProjectScope) -> TelegramGateway | None:
        """Resolve the bot that owns this project. `gateway` may be a single shared
        TelegramGateway (legacy, one bot for all projects) or a dict keyed by project
        name (one dedicated bot per project) — notifications must go out on the bot
        the project's users actually talk to."""
        if isinstance(self.gateway, dict):
            return self.gateway.get(scope.name)
        return self.gateway

    def _find_ready_task_nodes(
        self, scope: ProjectScope, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Query MemTrace for Task Nodes ready for unattended pickup, per the Task
        Node Schema judgment decided in ws_79bfe681/mem_99f7edc7 (criteria itself
        defined in ws_bff64012/mem_353f7454): tags contain both 'task' and
        'status:open', body.checkpoint=='build', body.current_stage=='dev', and
        body.gate_state.latest_verdict=='PASS' — i.e. the interactive session that
        settled the work already landed a Task Node in exactly this shape. The
        harness itself never judges "does this look settled"; it only recognizes
        nodes already in the ready shape. search_nodes has no structured tag/field
        filter, so the keyword query is just a recall net — _is_ready_task_node()
        is what actually decides. Sorted oldest-first (created_at ascending) as the
        default queue order — see run_scan_pass() for how that order is actually
        offered to the operator one task at a time."""
        if not self.memtrace_client:
            return []
        try:
            candidates = self.memtrace_client.search_nodes(
                workspace_id=scope.workspace_id,
                query="task status:open checkpoint:build current_stage:dev",
                limit=20,
                detail_level="full",
                run_id=run_id,
                stage="unattended_scan",
            )
            ready = [
                node
                for node in candidates
                if isinstance(node, dict) and node.get("id") and _is_ready_task_node(node)
            ]
            ready.sort(key=lambda n: str(n.get("created_at") or ""))
            return ready
        except Exception as exc:
            logger.error(f"Failed to scan backlog for workspace {scope.workspace_id}: {exc}")
            return []

    def scan_project_backlog(self, scope: ProjectScope, *, run_id: str | None = None) -> list[str]:
        """Convenience wrapper over _find_ready_candidates() for callers that only
        need ids (in queue order), e.g. status reporting."""
        return [str(node["id"]) for node in self._find_ready_candidates(scope, run_id=run_id)]

    def _find_ready_github_issues(self, scope: ProjectScope) -> list[dict[str, Any]]:
        """Alternative backlog source to MemTrace Task Nodes: GitHub Issues via the
        `gh` CLI, for any project whose harness-scope.md sets `github_repo`
        (2026-09-05 — the MemTrace Task Node tagging convention proved hard to
        keep in sync with actual work in practice, so git/GitHub became the source
        of truth for "what's ready to build" instead). Readiness judgment: an open
        issue with no assignee — anything still being discussed or already claimed
        (assigned to a human or a prior harness run) is excluded. Sorted
        oldest-first, same queue-order convention as _find_ready_task_nodes()."""
        if not scope.github_repo:
            return []
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", scope.github_repo,
                    "--state", "open",
                    "--json", "number,title,body,assignees,createdAt,url",
                    "--limit", "50",
                ],
                cwd=scope.working_directory,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                logger.error(
                    f"gh issue list failed for {scope.github_repo}: {result.stderr.strip()}"
                )
                return []
            issues = json.loads(result.stdout)
        except Exception as exc:
            logger.error(f"Failed to list GitHub issues for {scope.github_repo}: {exc}")
            return []
        if not isinstance(issues, list):
            return []
        ready = [
            issue
            for issue in issues
            if isinstance(issue, dict) and not issue.get("assignees")
        ]
        ready.sort(key=lambda issue: str(issue.get("createdAt") or ""))
        return [
            {
                "id": f"gh:{scope.github_repo}#{issue['number']}",
                "title": issue.get("title"),
                "created_at": issue.get("createdAt"),
                "body": issue.get("body"),
                "url": issue.get("url"),
                "number": issue.get("number"),
            }
            for issue in ready
            if issue.get("number") is not None
        ]

    def _find_ready_candidates(
        self, scope: ProjectScope, *, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Picks the project's configured backlog source: GitHub Issues if
        `github_repo` is set in its harness-scope.md, else the MemTrace Task Node
        judgment. A project uses one or the other, not both, so there's a single
        unambiguous queue order."""
        if scope.github_repo:
            return self._find_ready_github_issues(scope)
        return self._find_ready_task_nodes(scope, run_id=run_id)

    def run_scan_pass(self) -> dict[str, str]:
        results: dict[str, str] = {}
        for scope in self.projects:
            ws_id = scope.workspace_id
            # Check existing workspace lock or pending approval
            if self.trace_store.get_workspace_lock(ws_id) is not None:
                logger.info(f"Workspace {ws_id} is locked. Skipping scan.")
                results[ws_id] = "skipped_locked"
                continue

            conv_id = f"scan_{uuid4().hex[:8]}"
            locked = self.trace_store.acquire_workspace_lock(ws_id, conv_id)
            if not locked:
                logger.info(f"Failed to acquire lock for workspace {ws_id}. Skipping scan.")
                results[ws_id] = "skipped_locked"
                continue

            candidates = self._find_ready_candidates(scope, run_id=conv_id)
            declined = self.trace_store.list_recently_declined_stage_refs(ws_id, "unattended_write")
            candidates = [c for c in candidates if str(c.get("id")) not in declined]

            if not candidates:
                self.trace_store.release_workspace_lock(ws_id)
                results[ws_id] = "no_backlog"
                continue

            if not self.approval_mgr:
                # Discussion is the whole point of this flow now — without an
                # approval manager there's nowhere to send the question, so fail
                # closed (don't silently auto-run) rather than guess consent.
                self.trace_store.release_workspace_lock(ws_id)
                results[ws_id] = "no_approval_manager"
                continue

            # Only ever propose ONE Task Node at a time, oldest-first — the operator
            # discusses/decides on it via the approve/reject buttons, and the lock
            # stays held (see approval_mgr.request_approval below) for the whole
            # discussion+execution window. That serialization is also what makes an
            # overlapping scheduled scan pass land on "skipped_locked" instead of
            # queuing more work on top of an in-flight discussion or run.
            next_task = candidates[0]
            remaining = candidates[1:]
            next_id = str(next_task["id"])
            next_title = str(next_task.get("title") or next_id)
            results[ws_id] = f"found_{len(candidates)}_items"

            proposed_action = f"「{next_title}」（{next_id}）已定案可開發，是否核准開始？"
            if remaining:
                queue_preview = "、".join(
                    f"「{str(c.get('title') or c['id'])}」" for c in remaining[:5]
                )
                more = "…" if len(remaining) > 5 else ""
                proposed_action += (
                    f"\n\n佇列還有 {len(remaining)} 項待處理：{queue_preview}{more}"
                    "（拒絕這項會換下一項來問，不會整批一起做）。"
                )

            req = self.approval_mgr.request_approval(
                conversation_id=conv_id,
                workspace=ws_id,
                working_directory=str(scope.working_directory),
                reason="unattended_write",
                stage_ref=next_id,
                proposed_action=proposed_action,
                resume_goal=f"Implement planned backlog item: {next_id}",
            )
            gw = self._gateway_for(scope)
            if gw:
                gw.notify_approval_request(req)
            # Keep lock active while waiting for approval — released either by the
            # reject path (frees the workspace so the next scan pass can offer the
            # next candidate) or once the approved run finishes.
        return results
