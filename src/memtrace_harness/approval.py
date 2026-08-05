from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtrace_harness.trace_store import TraceStore


VALID_REASONS = {
    "gate_reject_twice",
    "reasoning_gap",
    "budget_exhausted",
    "out_of_scope",
    "ambiguous_requirement",
    "git_push",
    "unattended_write",
}


@dataclass
class ApprovalRequestData:
    id: str
    conversation_id: str
    workspace: str
    working_directory: str
    stage_ref: str | None
    reason: str
    proposed_action: str
    status: str
    created_at: str
    responded_at: str | None = None
    responded_by_chat_id: int | None = None

    def format_telegram_message(self) -> str:
        return (
            f"⚠️ **Approval Request** [{self.id}]\n"
            f"**Workspace**: `{self.workspace}`\n"
            f"**Reason**: {self.reason}\n"
            f"**Action**: {self.proposed_action}\n\n"
            f"Reply with:\n"
            f"`/approve {self.id}` to approve\n"
            f"`/reject {self.id} <reason>` to reject\n"
            f"`/clarify {self.id} <answer>` to clarify"
        )


class ApprovalManager:
    def __init__(self, trace_store: TraceStore, allowed_chat_ids: set[int]) -> None:
        self.trace_store = trace_store
        self.allowed_chat_ids = allowed_chat_ids

    def is_chat_id_allowed(self, chat_id: int) -> bool:
        if not self.allowed_chat_ids:
            return False
        return chat_id in self.allowed_chat_ids

    def request_approval(
        self,
        *,
        conversation_id: str,
        workspace: str,
        working_directory: str,
        reason: str,
        proposed_action: str,
        stage_ref: str | None = None,
    ) -> ApprovalRequestData:
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid approval reason: {reason}")
        req_id = self.trace_store.create_approval_request(
            conversation_id=conversation_id,
            workspace=workspace,
            working_directory=working_directory,
            reason=reason,
            proposed_action=proposed_action,
            stage_ref=stage_ref,
        )
        data = self.trace_store.get_approval_request(req_id)
        assert data is not None
        return ApprovalRequestData(**data)

    def get_request(self, request_id: str) -> ApprovalRequestData | None:
        data = self.trace_store.get_approval_request(request_id)
        if not data:
            return None
        return ApprovalRequestData(**data)

    def get_pending_for_conversation(self, conversation_id: str) -> ApprovalRequestData | None:
        data = self.trace_store.get_pending_approval_request(conversation_id)
        if not data:
            return None
        return ApprovalRequestData(**data)

    def respond(
        self, request_id: str, action: str, chat_id: int, reason_or_answer: str | None = None
    ) -> tuple[bool, str, ApprovalRequestData | None]:
        if not self.is_chat_id_allowed(chat_id):
            return False, "Chat ID not authorized", None

        req = self.get_request(request_id)
        if not req:
            return False, f"Approval request {request_id} not found", None

        if req.status != "pending":
            return False, f"Approval request {request_id} is already in state '{req.status}'", None

        status_map = {
            "approve": "approved",
            "reject": "rejected",
            "clarify": "approved",  # clarify provides human answer and resumes loop
        }
        new_status = status_map.get(action.lower())
        if not new_status:
            return False, f"Unknown action '{action}'", None

        success = self.trace_store.resolve_approval_request(
            request_id, new_status, responded_by_chat_id=chat_id
        )
        if not success:
            return False, "Failed to update approval request state", None

        updated_req = self.get_request(request_id)
        return True, f"Request {request_id} updated to {new_status}", updated_req
