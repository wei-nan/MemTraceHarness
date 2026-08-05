from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from memtrace_harness.adapter_factory import build_role_adapter_candidates
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schemas import TaskEnvelope

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalManager
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.scope import ProjectScope
    from memtrace_harness.telegram_gateway import TelegramGateway
    from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)


class UnattendedScanner:
    def __init__(
        self,
        config: HarnessConfig,
        trace_store: TraceStore,
        projects: list[ProjectScope],
        memtrace_client: MemTraceClient | None = None,
        gateway: TelegramGateway | None = None,
        approval_mgr: ApprovalManager | None = None,
    ) -> None:
        self.config = config
        self.trace_store = trace_store
        self.projects = projects
        self.memtrace_client = memtrace_client
        self.gateway = gateway
        self.approval_mgr = approval_mgr

    def scan_project_backlog(self, scope: ProjectScope) -> list[str]:
        """Query MemTrace for planned but not implemented nodes in project scope."""
        if not self.memtrace_client:
            return []
        try:
            res = self.memtrace_client.call_tool(
                "search_nodes",
                {
                    "workspace_id": scope.workspace_id,
                    "query": "planned NOT implemented",
                },
            )
            items = res.get("nodes", []) if isinstance(res, dict) else []
            return [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]
        except Exception as exc:
            logger.error(f"Failed to scan backlog for workspace {scope.workspace_id}: {exc}")
            return []

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

            backlog = self.scan_project_backlog(scope)
            if not backlog:
                self.trace_store.release_workspace_lock(ws_id)
                results[ws_id] = "no_backlog"
            else:
                results[ws_id] = f"found_{len(backlog)}_items"
                if self.config.unattended_write_requires_approval and self.approval_mgr:
                    req = self.approval_mgr.request_approval(
                        conversation_id=conv_id,
                        workspace=ws_id,
                        working_directory=str(scope.working_directory),
                        reason="unattended_write",
                        proposed_action=f"Scanner discovered {len(backlog)} backlog items ({', '.join(backlog[:3])}). Approve execution?",
                    )
                    if self.gateway:
                        self.gateway.notify_all_allowlisted(req.format_telegram_message())
                    # Keep lock active while waiting for approval!
                else:
                    # Execute loop immediately
                    try:
                        self.run_loop_for_backlog(scope, conv_id, backlog)
                    finally:
                        self.trace_store.release_workspace_lock(ws_id)
        return results

    def run_loop_for_backlog(self, scope: ProjectScope, conv_id: str, backlog: list[str]) -> None:
        goal = f"Implement planned backlog items: {', '.join(backlog)}"
        task = TaskEnvelope(
            task_id=f"task_{conv_id}",
            workspace_id=scope.workspace_id,
            goal=goal,
            context_refs=backlog,
            context_items=[],
            risk_level=scope.default_risk_level,
            source="unattended-scanner",
        )
        profiles = load_role_profiles()
        candidate_adapters = build_role_adapter_candidates(
            profiles=profiles,
            config=self.config,
            working_directory=scope.working_directory,
            timeout_seconds=self.config.cli_timeout_seconds,
        )
        runner = AgentLoopRunner(
            adapters={p_id: items[0] for p_id, items in candidate_adapters.items()},
            fallback_adapters={p_id: items[1:] for p_id, items in candidate_adapters.items()},
            role_profiles=profiles,
            trace_store=self.trace_store,
            memtrace_client=self.memtrace_client,
            approval_manager=self.approval_mgr,
        )
        summary = runner.run(task, writeback=True, conversation_id=conv_id)
        if self.gateway and self.gateway.primary_session_mgr:
            self.gateway.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="work_session_report",
                turn_type="dev_report",
                content=f"Scan loop {summary.conversation_id} completed with status {summary.status}: {summary.recommendation}",
                source_work_conversation_id=summary.conversation_id,
            )
        if self.gateway:
            self.gateway.notify_all_allowlisted(
                f"🤖 Unattended scan loop completed for workspace `{scope.workspace_id}`. Status: {summary.status}"
            )
