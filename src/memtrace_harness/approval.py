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
    resume_goal: str | None = None
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None

    def format_telegram_message(self) -> str:
        # No /approve or /reject lines: those are now inline-keyboard buttons attached
        # to this message (see TelegramGateway's approval-send path) — typing an ID is
        # no longer required for either. A direct reply to this message (Telegram's
        # native swipe-to-reply) is treated as a /clarify answer automatically; typing
        # /clarify explicitly still works too, for anyone not on a client with swipe-reply.
        return (
            f"⚠️ 核准請求 [{self.id}]\n"
            f"工作區：{self.workspace}\n"
            f"原因：{self.reason}\n"
            f"內容：{self.proposed_action}\n\n"
            f"可以直接點下方按鈕核准/拒絕，或直接回覆這則訊息說明你的想法（會當作補充說明繼續執行）。"
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
        resume_goal: str | None = None,
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
            resume_goal=resume_goal,
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

    def record_telegram_message(self, request_id: str, *, chat_id: int, message_id: int) -> None:
        self.trace_store.set_approval_telegram_message(
            request_id, chat_id=chat_id, message_id=message_id
        )

    def get_by_telegram_message(self, chat_id: int, message_id: int) -> ApprovalRequestData | None:
        """Resolve a swipe-reply back to the approval it's replying to — unambiguous,
        no ID typing or guessing required."""
        data = self.trace_store.get_approval_request_by_message_id(chat_id, message_id)
        if not data:
            return None
        return ApprovalRequestData(**data)

    def respond(
        self, request_id: str, action: str, chat_id: int, reason_or_answer: str | None = None
    ) -> tuple[bool, str, ApprovalRequestData | None]:
        if not self.is_chat_id_allowed(chat_id):
            return False, "此 chat ID 未經授權", None

        req = self.get_request(request_id)
        if not req:
            return False, f"找不到核准請求 {request_id}", None

        if req.status != "pending":
            return False, f"核准請求 {request_id} 已經是「{req.status}」狀態", None

        status_map = {
            "approve": "approved",
            "reject": "rejected",
            "clarify": "approved",  # clarify provides human answer and resumes loop
        }
        new_status = status_map.get(action.lower())
        if not new_status:
            return False, f"未知的操作「{action}」", None

        success = self.trace_store.resolve_approval_request(
            request_id, new_status, responded_by_chat_id=chat_id
        )
        if not success:
            return False, "更新核准請求狀態失敗", None

        updated_req = self.get_request(request_id)
        return True, f"請求 {request_id} 已更新為「{new_status}」", updated_req
