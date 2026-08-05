from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from pathlib import Path

from memtrace_harness.adapter_factory import build_role_adapter_candidates
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalManager, ApprovalRequestData
    from memtrace_harness.chat_triage import ChatTriage
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.primary_session import PrimarySessionManager
    from memtrace_harness.scope import ProjectScope

logger = logging.getLogger(__name__)


class TelegramGateway:
    def __init__(
        self,
        config: HarnessConfig,
        approval_manager: ApprovalManager,
        triage: ChatTriage,
        primary_session_mgr: PrimarySessionManager,
        projects: list[ProjectScope],
    ) -> None:
        self.config = config
        self.bot_token = config.telegram_bot_token
        self.allowed_chat_ids = config.telegram_allowed_chat_ids
        self.approval_manager = approval_manager
        self.triage = triage
        self.primary_session_mgr = primary_session_mgr
        self.projects = projects
        self.offset = 0

    def is_enabled(self) -> bool:
        return bool(self.bot_token)

    def send_message(self, chat_id: int, text: str) -> bool:
        if not self.is_enabled():
            return False
        if chat_id not in self.allowed_chat_ids:
            # Enforce allowlist: never send to non-allowlisted chat IDs
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram sendMessage failed: {exc}")
            return False

    def notify_all_allowlisted(self, text: str) -> None:
        for cid in self.allowed_chat_ids:
            self.send_message(cid, text)

    def get_updates(self, timeout: int = 10) -> list[dict[str, Any]]:
        if not self.is_enabled():
            return []

        url = f"https://api.telegram.org/bot{self.bot_token}/getUpdates?offset={self.offset}&timeout={timeout}"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout + 5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok"):
                    return data.get("result", [])
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram getUpdates failed: {exc}")
        return []

    def process_update(self, update: dict[str, Any]) -> str | None:
        update_id = update.get("update_id", 0)
        self.offset = max(self.offset, update_id + 1)

        message = update.get("message")
        if not message:
            return None

        chat_id = message.get("chat", {}).get("id")
        if chat_id is None or chat_id not in self.allowed_chat_ids:
            # HARD REQUIREMENT: Any chat ID not on the allowlist is ignored at transport layer
            return None

        text = message.get("text", "")
        if not text:
            return None

        result = self.triage.triage_message(text)

        if result.kind == "approval_response":
            assert result.approval_id is not None
            assert result.approval_action is not None
            success, msg, req_data = self.approval_manager.respond(
                request_id=result.approval_id,
                action=result.approval_action,
                chat_id=chat_id,
                reason_or_answer=result.approval_reason,
            )
            self.send_message(chat_id, f"Approval update: {msg}")
            if success and req_data:
                if req_data.status == "approved":
                    self._resume_approved_conversation(req_data, result.approval_reason)
                elif req_data.status in {"rejected", "expired"}:
                    self.approval_manager.trace_store.release_workspace_lock(req_data.workspace)
            return msg

        if result.kind == "out_of_scope" or result.kind == "unrecognized":
            rejection = result.rejection_message or "Request rejected (out of scope)."
            self.send_message(chat_id, f"❌ {rejection}")
            return rejection

        if result.kind == "status":
            info = f"Harness Gateway online.\nRegistered projects: {len(self.projects)}"
            self.send_message(chat_id, info)
            return info

        if result.kind == "task":
            scope = result.project_scope
            assert scope is not None
            # Record turn in primary session
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="user",
                turn_type="chat",
                content=text,
            )
            resp_msg = f"Task received for project '{scope.name}' (workspace `{scope.workspace_id}`). Goal: {result.task_goal}"
            self.send_message(chat_id, resp_msg)
            return resp_msg

    def _resume_approved_conversation(
        self, req_data: ApprovalRequestData, answer: str | None = None
    ) -> None:
        working_dir = Path(req_data.working_directory).resolve()
        if not working_dir.is_dir():
            logger.error(f"Cannot resume conversation {req_data.conversation_id}: working dir {working_dir} invalid")
            return
        trace_store = TraceStore(self.config.trace_db_path)
        task = TaskEnvelope(
            task_id=f"task_{req_data.conversation_id}",
            workspace_id=req_data.workspace,
            goal=f"Resume task after human approval for {req_data.reason} (answer: {answer or 'approved'})",
            context_refs=[],
            context_items=[],
            risk_level="medium",
            source="telegram-approval-resume",
        )
        profiles = load_role_profiles()
        candidate_adapters = build_role_adapter_candidates(
            profiles=profiles,
            config=self.config,
            working_directory=working_dir,
            timeout_seconds=self.config.cli_timeout_seconds,
        )
        runner = AgentLoopRunner(
            adapters={p_id: items[0] for p_id, items in candidate_adapters.items()},
            fallback_adapters={p_id: items[1:] for p_id, items in candidate_adapters.items()},
            role_profiles=profiles,
            trace_store=trace_store,
            approval_manager=self.approval_manager,
        )
        try:
            summary = runner.run(
                task,
                writeback=True,
                conversation_id=req_data.conversation_id,
            )
            matching_scope = next(
                (s for s in self.projects if s.workspace_id == req_data.workspace or s.working_directory.resolve() == working_dir),
                None,
            )
            project_name = matching_scope.name if matching_scope else req_data.workspace

            self.primary_session_mgr.record_turn(
                project=project_name,
                speaker="work_session_report",
                turn_type="dev_report",
                content=f"Resumed loop {summary.conversation_id} completed with status {summary.status}: {summary.recommendation}",
                source_work_conversation_id=summary.conversation_id,
            )
            self.notify_all_allowlisted(
                f"🔄 Resumed conversation `{req_data.conversation_id}`. Result: {summary.status} ({summary.recommendation[:120]})"
            )
        finally:
            trace_store.release_workspace_lock(req_data.workspace)
        updates = self.get_updates(timeout=1)
        processed = 0
        for update in updates:
            res = self.process_update(update)
            if res is not None:
                processed += 1
        return processed
