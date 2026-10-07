from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from memtrace_harness.decision_card import render_card

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
    "model_output_invalid",
    "config_change_required",
}

# These reasons mean "the loop stopped because it needs information only a human
# has" — the right response is an answer, not a yes/no. A plain approve here just
# resumes with the SAME unchanged goal, which (see the 2026-09-04 chat_3077a492
# incident: the same open questions never got answered across 6+ resume attempts)
# reliably reproduces the exact same stop rather than making progress. The other
# reasons (unattended_write, git_push, budget_exhausted, out_of_scope) really are a
# yes/no gate on a specific action, where approve/reject is the right primary CTA.
# model_output_invalid is deliberately NOT included here even though it's also a
# "needs_human" stop: it's a technical glitch (the model's own output didn't parse),
# not a question — there is nothing to answer, only retry (approve) or give up
# (reject), so it keeps the ordinary approve/reject framing instead.
INFO_NEEDED_REASONS = {"ambiguous_requirement", "reasoning_gap", "gate_reject_twice"}

# From this many earlier info-needed requests on the same conversation, the next one is
# prefixed with a "you've been asked this before" warning (see request_approval()).
REPEATED_QUESTION_THRESHOLD = 2


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
    decision_card: dict | None = None

    def format_telegram_message(self) -> str:
        # No /approve or /reject lines: those are now inline-keyboard buttons attached
        # to this message (see TelegramGateway's approval-send path) — typing an ID is
        # no longer required for either. A native swipe-reply to THIS message is how to answer
        # from plain text: the chat model reads it (see TelegramGateway.process_update()) and
        # resumes the loop only if the reply is an answer — a question to the model, or a reply
        # it is unsure about, leaves the task paused. The /approve, /reject, /clarify commands
        # and the buttons always work.
        if self.reason in INFO_NEEDED_REASONS and self.decision_card:
            # The stopped stage wrote its own question (see decision_card.py): show that
            # instead of the raw artifact dump, with a tappable button per option.
            return (
                f"❓ 需要你決定 [{self.id}]\n"
                f"工作區：{self.workspace}\n\n"
                f"{render_card(self.decision_card)}\n\n"
                f"👉 點下面的選項；想換個做法就滑動回覆（swipe-reply）這則訊息、直接寫下你的想法——"
                f"我會判斷是回答還是提問，只有回答才會帶著它繼續執行。想放棄這個任務按「放棄」。"
            )
        if self.reason in INFO_NEEDED_REASONS:
            # See INFO_NEEDED_REASONS: leads with "answer, don't tap a button" — a
            # bare approve here would just resume with this same unchanged goal.
            return (
                f"❓ 需要你回答問題 [{self.id}]\n"
                f"工作區：{self.workspace}\n"
                f"原因：{self.reason}\n"
                f"內容：{self.proposed_action}\n\n"
                f"👉 滑動回覆（swipe-reply）這則訊息、直接寫下你的答案——我會判斷你是在回答還是在問我問題，"
                f"只有回答才會帶著答案繼續執行；你問問題的話我只回答你，任務會繼續暫停。"
                f"不用按鈕，也不用特定格式。"
                f"如果想直接放棄這個任務，按下面的「放棄」。"
            )
        if self.reason == "config_change_required":
            # The stop is caused by a harness/project setting (e.g. agent_loop disabled
            # in the target project's harness-scope.md), not by missing information: no
            # approve and no typed answer can change that, so resuming with the same
            # goal just reproduces this exact stop (2026-09-29: 17 approve/answer rounds
            # on chat_3317bb36 / chat_0cf19653). Say so, and offer only "give up".
            return (
                f"🔧 需要先改設定，核准或回覆都無法解決 [{self.id}]\n"
                f"工作區：{self.workspace}\n"
                f"內容：{self.proposed_action}\n\n"
                f"這個任務被專案的設定擋住了，不是缺資訊——直接核准或回答，只會用同樣的設定再停一次。"
                f"請先修改設定（例如把該專案 harness-scope.md 的 agent_loop 改成 enabled），"
                f"改好之後重新下一次任務指令即可。這則請求可以按下方「放棄」關閉。"
            )
        if self.reason == "model_output_invalid":
            # See model_output_invalid's comment above: this is a parsing/schema
            # glitch, not a question — the "內容" below is the model's raw malformed
            # output, shown as evidence, not something to interpret or answer.
            return (
                f"⚙️ 系統技術性錯誤 [{self.id}]\n"
                f"工作區：{self.workspace}\n"
                f"這不是要問你問題——是這一步驟裡模型回傳的內容格式不對（不是有效 JSON，或缺少必要欄位），"
                f"系統看不懂，不是你需要理解或回答的東西。原始內容（供除錯參考，可以不用看懂）：\n"
                f"{self.proposed_action}\n\n"
                f"點下方「✅ 重試」會用同一個目標再跑一次這個階段（模型輸出格式問題通常換一次就會過）；"
                f"「❌ 放棄」則直接取消這個任務。"
            )
        return (
            f"⚠️ 核准請求 [{self.id}]\n"
            f"工作區：{self.workspace}\n"
            f"原因：{self.reason}\n"
            f"內容：{self.proposed_action}\n\n"
            f"可以直接點下方按鈕核准/拒絕；也可以滑動回覆（swipe-reply）這則訊息並寫下補充說明，"
            f"如果是補充說明就會帶著它繼續執行；如果你是在問問題，我只回答、任務繼續暫停。"
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
        decision_card: dict | None = None,
    ) -> ApprovalRequestData:
        if reason not in VALID_REASONS:
            raise ValueError(f"Invalid approval reason: {reason}")
        if reason in INFO_NEEDED_REASONS:
            earlier = self.trace_store.count_approval_requests(
                conversation_id, INFO_NEEDED_REASONS
            )
            if earlier >= REPEATED_QUESTION_THRESHOLD:
                # Each earlier request was resumed and the loop stopped on the same kind
                # of question again — the replies given so far aren't resolving it.
                proposed_action = (
                    f"⚠️ 這個任務已是第 {earlier + 1} 次因為同類問題停下來——先前的回覆沒有解決它。"
                    f"請這一次逐項寫出具體答案（不要只回「同意」），或直接放棄這個任務。\n\n"
                    f"{proposed_action}"
                )
        req_id = self.trace_store.create_approval_request(
            conversation_id=conversation_id,
            workspace=workspace,
            working_directory=working_directory,
            reason=reason,
            proposed_action=proposed_action,
            stage_ref=stage_ref,
            resume_goal=resume_goal,
            decision_card=decision_card,
        )
        data = self.trace_store.get_approval_request(req_id)
        assert data is not None
        return self._with_card(data)

    def _with_card(self, data: dict) -> ApprovalRequestData:
        return ApprovalRequestData(
            **data, decision_card=self.trace_store.get_approval_decision_card(data["id"])
        )

    def get_request(self, request_id: str) -> ApprovalRequestData | None:
        data = self.trace_store.get_approval_request(request_id)
        if not data:
            return None
        return self._with_card(data)

    def get_pending_for_conversation(self, conversation_id: str) -> ApprovalRequestData | None:
        data = self.trace_store.get_pending_approval_request(conversation_id)
        if not data:
            return None
        return self._with_card(data)

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
        return self._with_card(data)

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

        if (
            action.lower() == "approve"
            and req.reason in INFO_NEEDED_REASONS
            and not (reason_or_answer or "").strip()
        ):
            # See INFO_NEEDED_REASONS: a bare approve resumes with the same unchanged
            # goal and reproduces this exact stop (2026-09-30 chat_74826f4c looped on
            # it). Leave the request pending and say what is actually needed.
            return (
                False,
                f"請求 {request_id} 是在問問題，單純「核准」不會讓任務往前——同樣的問題會再出現一次。"
                "請滑動回覆（swipe-reply）該則訊息並寫下具體答案，或用 /clarify 帶上答案；"
                "想放棄就按「放棄」。",
                None,
            )

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
