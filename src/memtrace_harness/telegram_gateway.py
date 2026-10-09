from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from pathlib import Path

from memtrace_harness.adapter_factory import build_role_adapter_candidates
from memtrace_harness.completion_claims import (
    CHAT_NOTE_MARKER,
    CHAT_NOTE_TAGS,
    CLAIM_NO,
    CLAIM_OK,
    accept_claim,
    apply_kb_updates,
    chat_notes_to_ops,
    claim_keyboard,
    kb_updates_from_artifact,
    reject_claim,
)
from memtrace_harness.kb_gardening import (
    GardenOutcome,
    describe_outcome,
    undo_created_nodes,
    undo_gardening_run,
)
from memtrace_harness.kb_promotion import PromotionOutcome
from memtrace_harness.work_review import PROPOSAL_REASON, ReviewOutcome, describe_review
from memtrace_harness.kb_promotion import describe_outcome as describe_promotion
from memtrace_harness.decision_card import answer_for_choice, parse_pick_callback, pick_keyboard_rows
from memtrace_harness.decision_records import (
    CONTROLLER_OUTCOME_PREFIX,
    PRECEDENT_CONTENT_TYPE,
    decision_turn_text,
    precedent_block,
    record_resolution,
)
from memtrace_harness.trigger_review import (
    MAX_CONSECUTIVE_SILENT,
    ScheduleDelivery,
    build_schedule_prompt,
    decide_schedule_delivery,
    history_entry,
    silent_tag,
)
from memtrace_harness.approval import INFO_NEEDED_REASONS, ApprovalRequestData
from memtrace_harness.inflight import default_tracker
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.memory_digest import (
    DIGEST_TITLE_PREFIX,
    PREFERENCE_CORRECT_MARKER,
    apply_chat_preference_correction,
    preference_context_for,
)
from memtrace_harness.ops_mcp import ops_server_spec
from memtrace_harness.primary_session import SCHEDULE_REPORT, SCHEDULE_TRIGGER
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schedule import (
    ScheduleSpec,
    compute_next_run,
    describe_schedule,
    parse_schedule_spec,
    spec_from_row,
)
from memtrace_harness.schemas import ContextItem, TaskEnvelope
from memtrace_harness.taiwantrade_mcp import (
    DEFAULT_BASE_URL as TAIWANTRADE_DEFAULT_URL,
    ProxyError,
    cancel_order,
    load_api_key,
    mcp_server_spec,
    order_projects,
    submit_order,
)
from memtrace_harness.topic_recall import BRIEFS_HEADER, render_briefs_context
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.worktrees import TaskWorktree, WorktreeManager, can_use_worktrees

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalManager
    from memtrace_harness.chat_triage import ChatTriage
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.primary_session import PrimarySessionManager
    from memtrace_harness.scope import ProjectScope

logger = logging.getLogger(__name__)

# A run's record stays in the local trace store; it is not also written to the project's
# MemTrace workspace as a "Harness loop draft" node. Until 2026-10-07 every run was, so a
# schedule firing every ten minutes filled one project's specification workspace with 285
# near-identical nodes (95% of it). What a run teaches belongs in the knowledge base through the
# Controller's own kb_updates (completion_claims.py), not as one raw record per run.
LOOP_DRAFT_WRITEBACK = False

_DIGESTS_HEADER = (
    "Recent daily digests for this project — the Harness's nightly consolidation of "
    "the last few days (decisions, facts, items still open). Drafts grounded in the "
    "transcript, not accepted spec; the recent transcript below is newer than these"
)

# Telegram's sendMessage rejects any text over 4096 UTF-16 code units; stay comfortably
# under that in plain characters so multi-byte text (e.g. Chinese) never trips it.
TELEGRAM_MAX_MESSAGE_LENGTH = 3500
# The most of one Controller-kept overview (research directions, work-review record) that goes into a
# prompt; longer ones are cut, the full text stays in the knowledge base.
MAX_OVERVIEW_CHARS = 4500


def _split_telegram_text(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into <=limit-char chunks so a long report arrives as several readable
    messages instead of being truncated or rejected by Telegram. Breaks at the last
    paragraph/line boundary that fits, else the last sentence end, else the last space;
    only text with none of those (one unbroken run longer than `limit`) is cut hard."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"))
        if split_at < limit // 4:
            split_at = max(
                (window.rfind(mark) + len(mark) for mark in ("。", "！", "？", ". ", "! ", "? ")
                 if window.rfind(mark) != -1),
                default=-1,
            )
        if split_at < limit // 4:
            split_at = window.rfind(" ")
        if split_at < limit // 4:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


def _hydrate_github_issue_context(stage_ref: str, *, working_directory: Path) -> ContextItem | None:
    """Fetch a GitHub issue's real content for a stage_ref shaped "gh:owner/repo#N"
    (see scanner.py's _find_ready_github_issues()) via the `gh` CLI — the GitHub
    equivalent of hydrating a MemTrace Task Node's body, so Dev gets the actual
    issue description instead of just the bare "gh:..." id in the goal text."""
    body = stage_ref[len("gh:") :]
    repo, _, number = body.rpartition("#")
    if not repo or not number.isdigit():
        logger.error(f"Unrecognized GitHub issue stage_ref: {stage_ref}")
        return None
    try:
        result = subprocess.run(
            ["gh", "issue", "view", number, "--repo", repo, "--json", "title,body,url,number"],
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"gh issue view failed for {stage_ref}: {result.stderr.strip()}")
            return None
        issue = json.loads(result.stdout)
    except Exception:
        logger.exception(f"failed to hydrate GitHub issue context for {stage_ref}")
        return None
    return ContextItem(
        ref=stage_ref,
        title=f"GitHub issue #{issue.get('number')}: {issue.get('title')}",
        body=f"{issue.get('url')}\n\n{issue.get('body') or ''}",
        content_type="context",
        source="github",
    )


class TelegramGateway:
    def __init__(
        self,
        config: HarnessConfig,
        approval_manager: ApprovalManager,
        triage: ChatTriage,
        primary_session_mgr: PrimarySessionManager,
        projects: list[ProjectScope],
        memtrace_client: MemTraceClient | None = None,
        bot_token: str | None = None,
    ) -> None:
        self.config = config
        self.bot_token = bot_token or config.telegram_bot_token
        self.allowed_chat_ids = config.telegram_allowed_chat_ids
        self.approval_manager = approval_manager
        self.triage = triage
        self.primary_session_mgr = primary_session_mgr
        self.projects = projects
        self.memtrace_client = memtrace_client
        # The background topic-recall service (topic_recall.py). Wired in by the serve
        # command; None here means chat simply runs without the slow path, which is also
        # what keeps unit tests from ever starting a real model call in a thread.
        self.topic_recall = None
        # Builds the Controller's review call for a project (trigger_review.py); wired in by the
        # serve command. None means a schedule's result is pushed without being read first —
        # the behaviour before the review existed, and what keeps unit tests off real models.
        self.review_caller_factory: Callable[[str], Callable[[str], str | None]] | None = None
        # conversation_id -> the delivery chosen for a schedule run, handed from the run (which
        # records the result) to the thread that reports it.
        self._schedule_deliveries: dict[str, ScheduleDelivery] = {}
        # Starts a tidying pass over a project's knowledge-base workspaces now (the /garden
        # command); wired in by the serve command, None means the feature is not running here.
        self.garden_now: Callable[[ProjectScope, int], None] | None = None
        # Same for promoting cold-memory findings into the spec workspace (the /promote command).
        self.promote_now: Callable[[ProjectScope, int], None] | None = None
        # Same for the review of the harness's own work (/review, or asking for it in chat).
        self.work_review_now: Callable[[ProjectScope, int], None] | None = None
        # Each process invocation is a fresh instance (the gateway is a one-shot poll,
        # scheduled externally), so the update offset has to be persisted across runs —
        # otherwise every run would refetch and reprocess the same historical messages.
        self._offset_key = hashlib.sha256(self.bot_token.encode("utf-8")).hexdigest() if self.bot_token else None
        self.offset = (
            self.approval_manager.trace_store.get_telegram_offset(self._offset_key)
            if self._offset_key
            else 0
        )

    def is_enabled(self) -> bool:
        return bool(self.bot_token)

    def send_message(self, chat_id: int, text: str) -> bool:
        if not self.is_enabled():
            return False
        if chat_id not in self.allowed_chat_ids:
            # Enforce allowlist: never send to non-allowlisted chat IDs
            return False

        ok = True
        for chunk in _split_telegram_text(text):
            ok = self._send_single_message(chat_id, chunk) and ok
        return ok

    def _send_single_message(self, chat_id: int, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # Plain text, deliberately no parse_mode: most fields interpolated into these
        # messages (goal text, reasons, recommendations) are arbitrary/user-provided and
        # can contain unescaped Markdown special characters (e.g. "unattended_write" has
        # an underscore) — Telegram's legacy Markdown mode then fails the whole send with
        # "can't parse entities" and the message never arrives. Reliability beats styling.
        payload = {"chat_id": chat_id, "text": text}
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

    def send_message_with_keyboard(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]]
    ) -> int | None:
        """Same as send_message() but attaches a Telegram inline keyboard and returns
        the sent message's id (or None on failure) so the caller can remember which
        message a button/reply is about — see record_telegram_message()."""
        if not self.is_enabled() or chat_id not in self.allowed_chat_ids:
            return None
        chunks = _split_telegram_text(text)
        # Send any overflow as plain leading messages, so the keyboard ends up
        # attached to the final chunk where the operator will actually tap it.
        for chunk in chunks[:-1]:
            self._send_single_message(chat_id, chunk)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": chunks[-1],
            "reply_markup": {"inline_keyboard": keyboard},
        }
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result = data.get("result")
                if data.get("ok") and isinstance(result, dict):
                    return result.get("message_id")
                return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram sendMessage (with keyboard) failed: {exc}")
            return None

    def register_bot_commands(self) -> bool:
        """Tell Telegram what commands this bot supports (setMyCommands) so they show
        up in the client's own "/" command menu. There is no dedicated command to
        start a governed task any more — that now comes out of ordinary
        conversation with the model (see _chat_reply_and_maybe_start_task()).
        Idempotent and cheap; safe to call every gateway startup, not just once ever."""
        if not self.is_enabled():
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/setMyCommands"
        commands = [
            {"command": "status", "description": "查看目前狀態與可用指令"},
            {"command": "approve", "description": "核准待處理的請求（可加請求 ID）"},
            {"command": "reject", "description": "拒絕待處理的請求（可加請求 ID）"},
            {"command": "clarify", "description": "補充說明並繼續執行（可加請求 ID 與說明）"},
            {"command": "schedules", "description": "查看這個專案目前的排程"},
            {"command": "garden", "description": "現在就整理這個專案的知識庫（可加專案名）"},
            {"command": "promote", "description": "把冷記憶的研究結論整理進規格工作區（可加專案名）"},
            {"command": "review", "description": "復盤 harness 自己的工作：哪裡卡住、怎麼改進（可加專案名）"},
            {"command": "schedule_cancel", "description": "取消一組排程（需加排程 ID）"},
        ]
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps({"commands": commands}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram setMyCommands failed: {exc}")
            return False

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Required after handling a button tap — until this is called, Telegram keeps
        showing a loading spinner on the button the user just pressed."""
        if not self.is_enabled():
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram answerCallbackQuery failed: {exc}")

    def clear_message_keyboard(self, chat_id: int, message_id: int) -> None:
        """Best-effort: remove the approve/reject buttons once a request is resolved,
        so a stale button can't be tapped again after the request is no longer pending."""
        if not self.is_enabled():
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/editMessageReplyMarkup"
        payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram editMessageReplyMarkup failed: {exc}")

    @staticmethod
    def _approval_keyboard(
        request_id: str, reason: str, decision_card: dict | None = None
    ) -> list[list[dict[str, str]]]:
        if reason in INFO_NEEDED_REASONS and decision_card:
            # One button per option (a tap resumes with that option as the answer), then
            # the same way out as the question-only keyboard below.
            return [
                *pick_keyboard_rows(request_id, decision_card),
                [{"text": "🛑 放棄這個任務", "callback_data": f"reject:{request_id}"}],
            ]
        if reason in INFO_NEEDED_REASONS:
            # No "approve" button here — see INFO_NEEDED_REASONS: a bare approve
            # would just resume with the same unchanged goal that produced this
            # question in the first place. Answering is a text reply, not a tap.
            return [[{"text": "🛑 放棄這個任務", "callback_data": f"reject:{request_id}"}]]
        if reason == "config_change_required":
            # Only a way to close the request — see format_telegram_message(): neither
            # approving nor answering can lift a project-setting limit.
            return [[{"text": "🛑 放棄這個任務", "callback_data": f"reject:{request_id}"}]]
        if reason == "model_output_invalid":
            # Same approve/reject callback_data as the default case (resolve logic
            # is unchanged) — only the labels differ, since "approve" here means
            # "retry the same goal" rather than "go ahead with this write action".
            return [
                [
                    {"text": "🔁 重試", "callback_data": f"approve:{request_id}"},
                    {"text": "❌ 放棄", "callback_data": f"reject:{request_id}"},
                ]
            ]
        return [
            [
                {"text": "✅ 核准", "callback_data": f"approve:{request_id}"},
                {"text": "❌ 拒絕", "callback_data": f"reject:{request_id}"},
            ]
        ]

    def notify_approval_request(self, req: ApprovalRequestData) -> None:
        """Send an approval request with tappable buttons to every allowlisted chat,
        and remember the (chat_id, message_id) so a swipe-reply to it later resolves
        unambiguously back to this request. Note: with more than one allowlisted chat,
        only the last chat's message_id is kept for reply-matching — the common case
        here is a single operator/single chat, so this isn't built out further."""
        for cid in self.allowed_chat_ids:
            message_id = self.send_message_with_keyboard(
                cid,
                req.format_telegram_message(),
                self._approval_keyboard(req.id, req.reason, req.decision_card),
            )
            if message_id is not None:
                self.approval_manager.record_telegram_message(
                    req.id, chat_id=cid, message_id=message_id
                )

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Best-effort "typing..." indicator. Telegram only shows it for ~5s per call
        and there's no long-running work happening on this thread to refresh it from,
        so this is a one-shot hint, not a guarantee it stays up for the whole wait —
        the point is to signal "something is happening" immediately, not to be exact."""
        if not self.is_enabled() or chat_id not in self.allowed_chat_ids:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendChatAction"
        payload = {"chat_id": chat_id, "action": action}
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.error(f"Telegram sendChatAction failed: {exc}")

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

        callback_query = update.get("callback_query")
        if callback_query:
            return self._handle_callback_query(callback_query)

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
            return self._resolve_approval_action(
                result.approval_id, result.approval_action, chat_id, result.approval_reason
            )

        if result.kind == "out_of_scope" or result.kind == "unrecognized":
            rejection = result.rejection_message or "已拒絕（超出範圍）。"
            self.send_message(chat_id, f"❌ {rejection}")
            return rejection

        if result.kind == "review":
            msg = self._start_work_review(result.project_scope, chat_id)
            self.send_message(chat_id, msg)
            return msg

        if result.kind == "promote":
            scope = result.project_scope
            if scope is None:
                msg = "請指定專案，例如 /promote TWTradingStrategy。"
            elif self.promote_now is None:
                msg = "研究結論整理在這個 gateway 沒有啟用。"
            else:
                self.promote_now(scope, chat_id)
                msg = f"📚 開始把「{scope.name}」冷記憶裡的研究結論整理進規格工作區，完成後會回報（可能要幾分鐘）。"
            self.send_message(chat_id, msg)
            return msg

        if result.kind == "garden":
            scope = result.project_scope
            if scope is None:
                msg = "請指定要整理哪個專案的知識庫，例如 /garden Beri。"
            elif self.garden_now is None:
                msg = "知識庫整理在這個 gateway 沒有啟用。"
            else:
                self.garden_now(scope, chat_id)
                msg = f"🌱 開始整理「{scope.name}」的知識庫，完成後會回報（可能要幾分鐘）。"
            self.send_message(chat_id, msg)
            return msg

        if result.kind == "status":
            info = (
                f"Harness Gateway 已上線。\n已註冊專案數：{len(self.projects)}\n\n"
                "指令：\n"
                "/status — 看這則訊息\n"
                "一般打字：直接跟模型聊天即可，不用特定格式或前綴。如果對話讓模型確信你"
                "真的要開始開發，它會自己觸發受控任務，不需要另外核准；如果對話讓模型確信"
                "你要的是週期性重複執行，它會自己建立排程。\n"
                "待核准的請求（來自背景掃描或執行中任務暫停詢問）可以直接點訊息下方的按鈕，"
                "或用 /approve /reject /clarify <id>。\n"
                "/schedules — 查看這個專案目前的排程\n"
                "/schedule_cancel <id> — 取消一組排程"
            )
            self.send_message(chat_id, info)
            return info

        if result.kind == "schedule_list":
            scope = result.project_scope
            if scope is None:
                msg = "無法對應到任何已註冊的專案，請在訊息裡提到專案名稱。"
                self.send_message(chat_id, msg)
                return msg
            msg = self.list_schedules_message(scope)
            self.send_message(chat_id, msg)
            return msg

        if result.kind == "schedule_cancel":
            assert result.schedule_id is not None
            msg = self.cancel_schedule_message(result.schedule_id)
            self.send_message(chat_id, msg)
            return msg

        if result.kind == "chat":
            # Every plain message just goes to the model — no more separate
            # CHAT/QUESTION/LOOKUP/TASK classification pass, and no more "!"/"/task"
            # marker to mechanically queue a governed task (2026-09-10, explicit
            # user request: drop the whole triage layer, let the human and model
            # just talk, and let the model itself decide from the conversation
            # whether real development work should start).
            scope = result.project_scope
            assert scope is not None
            # Telegram attaches the full text of a swipe-replied-to message, whoever
            # sent it — including this bot's own scheduled pushes and notifications
            # from external scripts that use the same bot, neither of which the
            # Harness would otherwise ever see (a bot never receives its own messages).
            quoted = self._replied_to_text(message)
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="user",
                turn_type="chat",
                content=text if not quoted else f"{text}\n{self._quote_marker(quoted)}",
            )
            self.send_chat_action(chat_id, "typing")
            if self.topic_recall is not None:
                self.topic_recall.start(scope, chat_id, text)

            # A swipe-reply to a pending approval's own message may be the answer it is
            # waiting for — or a question about it, or an unrelated remark. Resuming a task
            # costs a whole loop (2026-10-05: a question typed as a swipe-reply started an
            # Opus planner run), so the chat model decides: it sees the approval and only
            # resumes the task by emitting HARNESS_APPROVAL_ANSWER::. When it isn't sure the
            # task stays paused (the buttons and /clarify still work).
            pending_approval = None
            reply_to = message.get("reply_to_message")
            if reply_to and reply_to.get("message_id") is not None:
                candidate = self.approval_manager.get_by_telegram_message(chat_id, reply_to["message_id"])
                if candidate and candidate.status == "pending":
                    pending_approval = candidate

            return self._chat_reply_and_maybe_start_task(
                scope, chat_id, text, quoted_text=quoted, pending_approval=pending_approval
            )

    # Stored with the user's turn so the transcript (and the nightly digest) still shows
    # what a short reply like "那檔怎麼了" was about. Kept after the user's own words so
    # the part the human actually typed reads first.
    _QUOTE_STORED_CHARS = 200
    _QUOTE_PROMPT_CHARS = 1500

    @classmethod
    def _quote_marker(cls, quoted: str) -> str:
        shown = quoted if len(quoted) <= cls._QUOTE_STORED_CHARS else quoted[: cls._QUOTE_STORED_CHARS] + "…"
        return f"（回覆的訊息：「{shown}」）"

    @classmethod
    def _quoted_reply_notice(cls, quoted: str | None) -> str:
        if not quoted:
            return ""
        shown = quoted if len(quoted) <= cls._QUOTE_PROMPT_CHARS else quoted[: cls._QUOTE_PROMPT_CHARS] + "…（截斷）"
        return (
            "使用者這則訊息是「回覆」下面這則訊息——可能是 Harness 的排程推播、你先前的回覆，"
            "或其他排程腳本發到這個聊天室的通知。請把使用者的話理解成針對它說的；它的內容就是"
            "你要依據的事實，不要說你看不到。\n"
            f"被回覆的訊息：\n「{shown}」\n\n"
        )

    @staticmethod
    def _replied_to_text(message: dict[str, Any]) -> str | None:
        reply_to = message.get("reply_to_message")
        if not isinstance(reply_to, dict):
            return None
        text = reply_to.get("text") or reply_to.get("caption")
        text = text.strip() if isinstance(text, str) else ""
        return text or None

    def _resolve_approval_action(
        self,
        request_id: str,
        action: str,
        chat_id: int,
        reason_or_answer: str | None,
        chosen_index: int | None = None,
    ) -> str:
        """Shared by the /approve|/reject|/clarify text commands, the inline-keyboard
        button callbacks, and a TASK-classified plain message (see
        _classify_message_intent()) — one place that resolves an approval and
        reacts to the outcome, so those entry points can't drift out of sync."""
        success, msg, req_data = self.approval_manager.respond(
            request_id=request_id, action=action, chat_id=chat_id, reason_or_answer=reason_or_answer
        )
        self.send_message(chat_id, f"核准狀態更新：{msg}")
        if success and req_data:
            self._record_decision(req_data, action, reason_or_answer, chosen_index)
            if req_data.telegram_chat_id is not None and req_data.telegram_message_id is not None:
                self.clear_message_keyboard(req_data.telegram_chat_id, req_data.telegram_message_id)
            if req_data.status == "approved":
                self._resume_approved_conversation(req_data, reason_or_answer)
            elif req_data.status in {"rejected", "expired"}:
                self.approval_manager.trace_store.release_workspace_lock(
                    req_data.workspace, req_data.conversation_id
                )
        return msg

    def _record_decision(
        self,
        req_data: ApprovalRequestData,
        action: str,
        answer: str | None,
        chosen_index: int | None,
    ) -> None:
        """Keep what the operator just decided as precedent (decision_records.py). Best
        effort: the resolution has already happened, so a failure here is logged, not raised."""
        scope = next(
            (
                p
                for p in self.projects
                if p.workspace_id == req_data.workspace
                or str(p.working_directory) == req_data.working_directory
            ),
            None,
        )
        if scope is None:
            return
        try:
            recorded = record_resolution(
                self.approval_manager.trace_store,
                req_data,
                project=scope.name,
                action=action,
                answer=answer,
                chosen_index=chosen_index,
            )
            if recorded:
                # A button tap leaves no message of the operator's own; log it as their turn
                # so the chat model and the nightly digest see it like anything else they said.
                self.primary_session_mgr.record_turn(
                    project=scope.name, speaker="user", turn_type="decision", content=recorded[1]
                )
        except Exception:
            logger.exception(f"recording the decision on {req_data.id} failed; continuing")

    def expire_stale_approvals(self, ttl_hours: int, now: datetime | None = None) -> list[str]:
        """Close this gateway's pending approvals older than `ttl_hours`: mark them
        expired, strip their Telegram buttons, and tell the operator once. Workspace
        locks are left alone — a lock belongs to whatever run holds it now — except
        the one an unattended scan took while proposing the task: that lock exists only
        to hold the question, so once it expires nothing owns it and it would block every
        later scan. Returns the expired request ids."""
        if ttl_hours <= 0:
            return []
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=ttl_hours)
        workspaces = {p.workspace_id for p in self.projects}
        stale = self.approval_manager.trace_store.list_stale_pending_approvals(
            workspaces, cutoff.isoformat()
        )
        expired: list[str] = []
        for data in stale:
            if not self.approval_manager.trace_store.resolve_approval_request(data["id"], "expired"):
                continue
            expired.append(data["id"])
            if data.get("reason") == "unattended_write" and str(data["conversation_id"]).startswith("scan_"):
                self.approval_manager.trace_store.release_workspace_lock(
                    data["workspace"], data["conversation_id"]
                )
            if data.get("telegram_chat_id") is not None and data.get("telegram_message_id") is not None:
                self.clear_message_keyboard(data["telegram_chat_id"], data["telegram_message_id"])
        if expired:
            self.notify_all_allowlisted(
                f"⌛ {len(expired)} 筆超過 {ttl_hours} 小時沒回應的請求已自動失效"
                f"（{', '.join(expired)}）。它們的按鈕已移除；如果任務還需要，請重新下指令。"
            )
        return expired

    def _handle_callback_query(self, callback_query: dict[str, Any]) -> str | None:
        """A tap on an approve/reject inline-keyboard button. Telegram delivers this as
        its own update type (not a "message"), separate from ordinary text updates."""
        callback_id = callback_query.get("id")
        data = callback_query.get("data") or ""
        source_message = callback_query.get("message") or {}
        chat_id = source_message.get("chat", {}).get("id")
        if chat_id is None or chat_id not in self.allowed_chat_ids:
            if callback_id:
                self.answer_callback_query(callback_id)
            return None
        pick = parse_pick_callback(data)
        if pick is not None:
            result = self._resolve_card_pick(pick[0], pick[1], chat_id)
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        action, _, request_id = data.partition(":")
        if not action or not request_id:
            if callback_id:
                self.answer_callback_query(callback_id, text="無法辨識的按鈕")
            return None
        if action == "kb_undo":
            result = self._resolve_garden_undo(request_id, chat_id, source_message.get("message_id"))
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        if action in {CLAIM_OK, CLAIM_NO}:
            result = self._resolve_claim(action, request_id, chat_id, source_message.get("message_id"))
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        if action in {"sched_resume", "sched_cancel"}:
            result = self._resolve_schedule_button(
                action.removeprefix("sched_"), request_id, chat_id, source_message.get("message_id")
            )
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        if action in {"job_confirm", "job_cancel"}:
            result = self._resolve_job_action(
                action, request_id, chat_id, source_message.get("message_id")
            )
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        if action in {"order_confirm", "order_cancel"}:
            result = self._resolve_order_action(
                action, request_id, chat_id, source_message.get("message_id")
            )
            if callback_id:
                self.answer_callback_query(callback_id, text=result[:200])
            return result
        result = self._resolve_approval_action(request_id, action, chat_id, None)
        if callback_id:
            self.answer_callback_query(callback_id, text=result[:200])
        return result

    def _resolve_card_pick(self, request_id: str, index: int, chat_id: int) -> str:
        """A tap on one option of a decision card: resume the stopped task with that option
        as the operator's answer, exactly as if they had typed it to /clarify."""
        req = self.approval_manager.get_request(request_id)
        if req is None or not req.decision_card:
            return f"找不到請求 {request_id} 的選項"
        answer = answer_for_choice(req.decision_card, index)
        if answer is None:
            return "無法辨識的選項"
        return self._resolve_approval_action(
            request_id, "clarify", chat_id, answer, chosen_index=index
        )

    # ---- orders the model proposed (see taiwantrade_mcp.py): shown here, sent only on a tap

    @staticmethod
    def _order_summary(order: dict) -> str:
        side = "買進" if order["action"] == "Buy" else "賣出"
        unit = "股（盤中零股）" if order["is_odd_lot"] else "張"
        text = (
            f"{side} {order['symbol']}｜{order['quantity']} {unit}｜限價 {order['price']:g}"
            f"｜預估金額 {order['estimated_value']:,.0f} 元"
        )
        if order.get("kind") == "cancel":
            return f"撤銷委託 {order['target_order_id']}（{text}）"
        return text

    @staticmethod
    def _order_keyboard(intent_id: str, kind: str = "order") -> list[list[dict[str, str]]]:
        confirm, abort = ("✅ 確認撤單", "❌ 不撤單") if kind == "cancel" else ("✅ 確認下單", "❌ 取消")
        return [
            [
                {"text": confirm, "callback_data": f"order_confirm:{intent_id}"},
                {"text": abort, "callback_data": f"order_cancel:{intent_id}"},
            ]
        ]

    @staticmethod
    def _order_expired(order: dict) -> bool:
        try:
            expires = datetime.fromisoformat(str(order["expires_at"]).replace("Z", "+00:00"))
        except ValueError:
            return False  # TaiwanTrade enforces the real deadline when the order is submitted
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    def _record_order_turn(self, project: str, content: str) -> None:
        """Write the outcome into the project's chat log so the chat model knows what
        happened to the order it proposed (it never sees the confirmation itself)."""
        self.primary_session_mgr.record_turn(
            project=project, speaker="system", turn_type="dev_report", content=content
        )

    def process_order_intents(self) -> None:
        """Gateway tick: put newly proposed orders in front of the human, and close the
        ones nobody confirmed in time."""
        trace_store = self.approval_manager.trace_store
        for order in trace_store.claim_pending_order_intents([p.name for p in self.projects]):
            if order["kind"] == "cancel":
                text = (
                    f"🗑 待確認撤單（{order['project']}）\n"
                    f"{self._order_summary(order)}\n"
                    f"有效至：{order['expires_at']}\n\n"
                    "這是模型提出的撤單，還沒有送到券商。按「確認撤單」才會送出；不按、按「不撤單」、"
                    "或時間到都不會撤單。"
                )
            else:
                text = (
                    f"🛒 待確認委託（{order['project']}）\n"
                    f"{self._order_summary(order)}\n"
                    f"有效至：{order['expires_at']}\n\n"
                    "這是模型提出的委託，還沒有送到券商。按「確認下單」才會送出；不按、按取消、"
                    "或時間到都不會下單。"
                )
            shown = False
            if order["telegram_message_id"] is not None and order["telegram_chat_id"] is not None:
                # Re-shown: the earlier buttons are stale clutter higher up the chat.
                self.clear_message_keyboard(order["telegram_chat_id"], order["telegram_message_id"])
            for cid in self.allowed_chat_ids:
                message_id = self.send_message_with_keyboard(
                    cid, text, self._order_keyboard(order["intent_id"], order["kind"])
                )
                if message_id is not None:
                    trace_store.set_order_intent_message(
                        order["intent_id"], chat_id=cid, message_id=message_id
                    )
                    shown = True
            if not shown:
                # Telegram unreachable: put it back so the next tick tries again.
                trace_store.transition_order_intent(order["intent_id"], ("awaiting",), "pending")
        for order in trace_store.expire_order_intents(datetime.now(timezone.utc).isoformat()):
            if order["telegram_message_id"] is not None and order["telegram_chat_id"] is not None:
                self.clear_message_keyboard(order["telegram_chat_id"], order["telegram_message_id"])
                self.send_message(
                    order["telegram_chat_id"],
                    f"⌛ {'撤單' if order['kind'] == 'cancel' else '委託'}已過期，沒有"
                    f"{'撤單' if order['kind'] == 'cancel' else '下單'}：{self._order_summary(order)}",
                )
            self._record_order_turn(
                order["project"],
                f"{'撤單' if order['kind'] == 'cancel' else '委託'}已過期、沒有"
                f"{'撤單' if order['kind'] == 'cancel' else '下單'}：{self._order_summary(order)}",
            )

    # ---- jobs the model asked to re-run (see ops_mcp.py): shown here, run only on a tap
    JOB_TIMEOUT_SECONDS = 900
    JOB_OUTPUT_CHARS = 1500

    def process_job_requests(self) -> None:
        """Gateway tick: put newly requested jobs in front of the human; close the unanswered."""
        trace_store = self.approval_manager.trace_store
        for job in trace_store.claim_pending_job_requests([p.name for p in self.projects]):
            text = (
                f"▶️ 待確認重跑工作（{job['project']}）\n"
                f"{job['job_name']}：{job['command']}\n"
                f"有效至：{job['expires_at']}\n\n"
                "這是模型提出的要求，還沒有執行。按「確認執行」才會跑；不按、按取消、或時間到都不會跑。"
            )
            keyboard = [[
                {"text": "✅ 確認執行", "callback_data": f"job_confirm:{job['request_id']}"},
                {"text": "❌ 取消", "callback_data": f"job_cancel:{job['request_id']}"},
            ]]
            shown = False
            for cid in self.allowed_chat_ids:
                message_id = self.send_message_with_keyboard(cid, text, keyboard)
                if message_id is not None:
                    trace_store.set_job_request_message(job["request_id"], chat_id=cid, message_id=message_id)
                    shown = True
            if not shown:
                trace_store.transition_job_request(job["request_id"], ("awaiting",), "pending")
        for job in trace_store.expire_job_requests(datetime.now(timezone.utc).isoformat()):
            if job["telegram_message_id"] is not None and job["telegram_chat_id"] is not None:
                self.clear_message_keyboard(job["telegram_chat_id"], job["telegram_message_id"])
            msg = f"⌛ 重跑要求已過期、沒有執行：{job['job_name']}"
            if job["telegram_chat_id"] is not None:
                self.send_message(job["telegram_chat_id"], msg)
            self._record_order_turn(job["project"], msg)

    def _resolve_job_action(
        self, action: str, request_id: str, chat_id: int, message_id: int | None
    ) -> str:
        trace_store = self.approval_manager.trace_store
        job = trace_store.get_job_request(request_id)
        scope = next((p for p in self.projects if job and p.name == job["project"]), None)
        if job is None or scope is None:
            return "找不到這個要求。"
        if message_id is not None:
            self.clear_message_keyboard(chat_id, message_id)
        if action == "job_cancel":
            if not trace_store.transition_job_request(request_id, ("awaiting",), "cancelled"):
                return f"這個要求已經是「{job['status']}」，沒有變動。"
            trace_store.finish_job_request(request_id, "cancelled")
            result = f"❌ 已取消，沒有執行：{job['job_name']}"
            self.send_message(chat_id, result)
            self._record_order_turn(job["project"], result)
            return result
        # Compare-and-set: a double tap cannot start it twice. The command is the one declared
        # in the scope file now, not whatever text the request carried.
        if not trace_store.transition_job_request(request_id, ("awaiting",), "running"):
            return f"這個要求已經是「{job['status']}」，沒有再執行。"
        command = (scope.jobs or {}).get(job["job_name"])
        if command is None:
            trace_store.finish_job_request(request_id, "failed", {"error": "job no longer declared"})
            return f"工作 {job['job_name']} 已不在專案設定裡，沒有執行。"
        threading.Thread(
            target=self._run_job, args=(scope, job, command, chat_id), daemon=True,
            name=f"job-{request_id}",
        ).start()
        started = f"▶️ 開始執行 {job['job_name']}，完成後會回報。"
        self.send_message(chat_id, started)
        return started

    def _run_job(self, scope: ProjectScope, job: dict, command: str, chat_id: int) -> None:
        trace_store = self.approval_manager.trace_store
        try:
            done = subprocess.run(
                command, shell=True, cwd=scope.working_directory, capture_output=True, text=True,
                timeout=self.JOB_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
            )
            output = ((done.stdout or "") + (done.stderr or "")).strip()
            status, code = ("done" if done.returncode == 0 else "failed"), done.returncode
        except subprocess.TimeoutExpired:
            output, status, code = f"超過 {self.JOB_TIMEOUT_SECONDS} 秒，已中止。", "failed", None
        except Exception as exc:  # noqa: BLE001 — report any launch failure to the human
            output, status, code = f"無法啟動：{exc}", "failed", None
        tail = output[-self.JOB_OUTPUT_CHARS:]
        trace_store.finish_job_request(job["request_id"], status, {"exit_code": code, "output_tail": tail})
        head = "✅ 完成" if status == "done" else f"❌ 失敗（結束碼 {code}）"
        result = f"{head}：{job['job_name']}\n{tail}"
        self.send_message(chat_id, result)
        self._record_order_turn(job["project"], result)

    def _resolve_order_action(
        self, action: str, intent_id: str, chat_id: int, message_id: int | None
    ) -> str:
        trace_store = self.approval_manager.trace_store
        order = trace_store.get_order_intent(intent_id)
        if order is None or order["project"] not in {p.name for p in self.projects}:
            return "找不到這筆委託。"
        summary = self._order_summary(order)
        if action == "order_cancel":
            if not trace_store.transition_order_intent(intent_id, ("awaiting",), "cancelled"):
                return f"這筆委託已經是「{order['status']}」，沒有變動。"
            trace_store.finish_order_intent(intent_id, "cancelled")
            result = (
                f"❌ 已取消，沒有撤單：{summary}"
                if order["kind"] == "cancel"
                else f"❌ 已取消，沒有下單：{summary}"
            )
        else:
            result = self._confirm_order(order, summary)
        if message_id is not None:
            self.clear_message_keyboard(chat_id, message_id)
        self.send_message(chat_id, result)
        self._record_order_turn(order["project"], result)
        return result

    def _confirm_order(self, order: dict, summary: str) -> str:
        """The human tapped confirm: send the order. Everything past the compare-and-set
        runs at most once per intent, so a double tap cannot place it twice."""
        trace_store = self.approval_manager.trace_store
        intent_id = order["intent_id"]
        if not trace_store.transition_order_intent(intent_id, ("awaiting",), "confirming"):
            current = trace_store.get_order_intent(intent_id)
            return f"這筆委託已經是「{current['status'] if current else '未知'}」，沒有再送出。"
        if self._order_expired(order):
            trace_store.finish_order_intent(intent_id, "expired")
            return f"⌛ 已過期，沒有送出：{summary}"
        if order["kind"] == "cancel":
            return self._send_cancel(order, summary)
        try:
            placed = submit_order(
                intent_id,
                order["confirmation_token"],
                base_url=os.getenv("HARNESS_TAIWANTRADE_API_URL", TAIWANTRADE_DEFAULT_URL),
                api_key=load_api_key(),
            )
        except ProxyError as exc:
            trace_store.finish_order_intent(intent_id, "failed", {"error": str(exc)})
            warning = (
                "\n⚠️ 券商那邊的狀態不明，請先到券商 App 確認有沒有這筆委託，不要重複下單。"
                if "HTTP 502" in str(exc)
                else ""
            )
            return f"❌ 委託沒有成功送出：{summary}\n原因：{exc}{warning}"
        trace_store.finish_order_intent(intent_id, "submitted", placed)
        filled = placed.get("filled_qty")
        return (
            f"✅ 已送出委託：{summary}\n"
            f"券商委託編號：{placed.get('order_id', '?')}｜狀態：{placed.get('status', '?')}"
            + (f"｜已成交 {filled}" if filled else "")
            + "\n成交結果之後可以問我（我會用 list_orders 幫你查）。"
        )

    def _send_cancel(self, order: dict, summary: str) -> str:
        """The human tapped confirm on a cancel proposal: send the DELETE (once — the caller
        already won the compare-and-set)."""
        trace_store = self.approval_manager.trace_store
        intent_id = order["intent_id"]
        try:
            sent = cancel_order(
                order["target_order_id"],
                base_url=os.getenv("HARNESS_TAIWANTRADE_API_URL", TAIWANTRADE_DEFAULT_URL),
                api_key=load_api_key(),
            )
        except ProxyError as exc:
            trace_store.finish_order_intent(intent_id, "failed", {"error": str(exc)})
            return (
                f"❌ 撤單沒有成功送出：{summary}\n原因：{exc}\n"
                "（如果原因是委託已成交或已不存在，代表它已經不能撤了——到券商 App 或請我用 "
                "get_positions 確認持股。）"
            )
        trace_store.finish_order_intent(intent_id, "submitted", sent)
        return (
            f"✅ 已送出撤單要求：{summary}\n"
            "撤單是否真的生效要等券商回報；稍後可以請我查（list_orders，並對照 get_positions）。"
        )

    def _start_or_queue_task(
        self, scope: ProjectScope, goal: str, chat_id: int, schedule_id: str | None = None
    ) -> str:
        """Start a governed task now if the project has a free worker slot, otherwise
        queue it. A schedule whose previous run is still running (or still queued) is
        skipped instead — it must never pile up behind itself."""
        ws_id = scope.workspace_id
        trace_store = self.approval_manager.trace_store
        conv_id = f"chat_{uuid4().hex[:8]}"
        slot_limit = self._slot_limit(scope)
        outcome = trace_store.acquire_task_slot(
            ws_id, conv_id, max_slots=slot_limit, schedule_id=schedule_id
        )
        if outcome == "duplicate_schedule":
            logger.info(f"schedule {schedule_id} is still running; skipping this occurrence")
            return f"排程 {schedule_id} 上一輪尚未結束，本輪略過。"
        if outcome != "acquired":
            queue_id = trace_store.enqueue_task(
                workspace_id=ws_id,
                kind="new",
                payload={"goal": goal},
                conversation_id=None,
                schedule_id=schedule_id,
                chat_id=chat_id,
            )
            position = sum(1 for q in trace_store.list_queued_tasks(ws_id) if q["id"] <= queue_id)
            msg = (
                f"「{scope.name}」的 {slot_limit} 個任務位都在忙，這個任務已排入佇列"
                f"（第 {position} 位），有空位時會自動開始。"
            )
            self.send_message(chat_id, msg)
            return msg

        # No separate approval step before starting: the decision to do real
        # development work is made in conversation with the model itself (see
        # _chat_reply_and_maybe_start_task()) — by the time this is called, that
        # decision has already been made together with the human in the chat.
        # Say WHICH task started: with several schedules (and chat tasks) running, a bare
        # "a task started" can't be told apart.
        self.send_message(chat_id, self._task_started_text(scope, goal, schedule_id))
        self._launch_task(scope, conv_id, goal, chat_id, schedule_id)
        return f"任務已在背景開始執行：{goal}"

    @staticmethod
    def _task_started_text(scope: ProjectScope, goal: str, schedule_id: str | None) -> str:
        if schedule_id:
            return f"⏰ 排程 {schedule_id} 開始執行：{goal}"
        return f"🔧 開始執行「{scope.name}」：{goal}"

    def _launch_task(
        self, scope: ProjectScope, conv_id: str, goal: str, chat_id: int, schedule_id: str | None
    ) -> None:
        """Run an already-slotted task in a background thread so this bot's poll loop —
        and therefore ordinary chat on it — isn't blocked for the minutes a governed loop
        can take. The slot (acquired by the caller) is what keeps a project within its
        worker limit; it is released here once the run finishes, which also starts the
        next queued task."""
        ws_id = scope.workspace_id
        trace_store = self.approval_manager.trace_store

        def _run() -> None:
            try:
                summary = self._run_new_task(scope, conv_id, goal, schedule_id=schedule_id)
                delivery = self._schedule_deliveries.pop(conv_id, None) if schedule_id else None
                if delivery is not None and delivery.action == "digest_only":
                    # Recorded in the chat log (tagged) for the nightly digest; not pushed.
                    logger.info(f"schedule {schedule_id} result held back for the digest: {delivery.reason}")
                    return
                if delivery is not None and delivery.action == "pause_and_notify":
                    self._pause_and_notify(scope, schedule_id, summary, delivery, chat_id)
                    return
                if schedule_id and summary.status == "needs_human":
                    # A notification, not a question: no approval is open, so a later reply
                    # can't resume this monitoring run with some other goal.
                    msg = (
                        f"⏰ 排程 {schedule_id} 有事需要你留意：{summary.recommendation}\n\n"
                        "這只是通知，沒有待核准的問題；要處理的話直接在聊天告訴我。\n\n"
                        f"🧩 {self._model_summary(summary)}"
                    )
                else:
                    msg = (
                        f"✅「{scope.name}」的任務已完成。狀態：{summary.status}。{summary.recommendation}\n\n"
                        f"🧩 {self._model_summary(summary)}"
                    )
                self._report_run_outcome(
                    conv_id=conv_id, summary=summary, final_text=msg, chat_id=chat_id
                )
            except Exception:
                logger.exception(f"background agent loop for '{scope.name}' ({conv_id}) failed")
                self.send_message(chat_id, f"⚠️「{scope.name}」的任務執行時發生未預期錯誤，請查看日誌。")
                if schedule_id:
                    # The human just got a failure notice for this schedule; without a
                    # row the chat model would know nothing about it.
                    self.primary_session_mgr.record_turn(
                        project=scope.name,
                        speaker="work_session_report",
                        turn_type=SCHEDULE_REPORT,
                        content=f"排程 {schedule_id} 執行失敗（loop {conv_id}，未預期錯誤，詳見日誌）。",
                        source_work_conversation_id=conv_id,
                        schedule_id=schedule_id,
                    )
            finally:
                trace_store.release_workspace_lock(ws_id, conv_id)
                default_tracker.unregister(thread)
                self._drain_task_queue(scope)

        thread = threading.Thread(target=_run, name=f"agent-loop-{conv_id}", daemon=True)
        thread.start()
        default_tracker.register(
            thread, workspace_id=ws_id, conversation_id=conv_id, trace_store=trace_store
        )

    def drain_task_queues(self) -> None:
        """Start queued tasks wherever a worker slot is free. Also called on the gateway
        tick, so work queued before a restart is picked up again."""
        trace_store = self.approval_manager.trace_store
        queued = set(trace_store.queued_workspace_ids())
        for scope in self.projects:
            if scope.workspace_id in queued:
                self._drain_task_queue(scope)

    def _drain_task_queue(self, scope: ProjectScope) -> None:
        trace_store = self.approval_manager.trace_store
        ws_id = scope.workspace_id
        try:
            while True:
                item = trace_store.peek_next_queued_task(ws_id)
                if item is None:
                    return
                conv_id = item["conversation_id"] or f"chat_{uuid4().hex[:8]}"
                if (
                    trace_store.acquire_task_slot(
                        ws_id, conv_id, max_slots=self._slot_limit(scope),
                        schedule_id=item["schedule_id"], check_queue=False,
                    )
                    != "acquired"
                ):
                    return
                if not trace_store.claim_queued_task(item["id"]):
                    trace_store.release_workspace_lock(ws_id, conv_id)  # another drainer took it
                    continue
                try:
                    self._start_queued_item(scope, item, conv_id)
                except Exception:
                    logger.exception(f"could not start queued task {item['id']} for '{scope.name}'")
                    trace_store.release_workspace_lock(ws_id, conv_id)
        except Exception:
            logger.exception(f"draining the task queue for '{scope.name}' failed")

    def _start_queued_item(self, scope: ProjectScope, item: dict, conv_id: str) -> None:
        chat_id = item["chat_id"]
        if item["kind"] == "resume":
            req = self.approval_manager.get_request(item["payload"]["request_id"])
            if req is None or req.status != "approved":
                self.approval_manager.trace_store.release_workspace_lock(scope.workspace_id, conv_id)
                return
            self._launch_resume(req, item["payload"].get("answer"))
            return
        goal = item["payload"]["goal"]
        if chat_id is not None:
            self.send_message(
                chat_id, f"▶️ 輪到排隊中的任務了，開始執行「{scope.name}」：{goal[:80]}"
            )
        self._launch_task(scope, conv_id, goal, chat_id, item["schedule_id"])

    # ---- per-task git worktrees (see worktrees.py)

    def _slot_limit(self, scope: ProjectScope) -> int:
        """How many tasks of this project may run at once: what harness-scope.md says. A
        project that can't give each task its own worktree (not a git repo, or no commit
        yet) still runs that many side by side; only its *development* is serialized, by
        the edit lock (see _edit_lock_for)."""
        return max(1, scope.max_workers)

    def _edit_lock_for(
        self, scope: ProjectScope, conv_id: str, worktree: TaskWorktree | None, trace_store: TraceStore
    ) -> tuple[Callable[[], bool], Callable[[], None]] | None:
        """A task that has its own worktree, or the whole project to itself, edits freely.
        Otherwise it shares one folder with its siblings and takes turns to develop."""
        if worktree is not None or self._slot_limit(scope) <= 1:
            return None
        ws_id = scope.workspace_id
        return (
            lambda: trace_store.acquire_edit_lock(ws_id, conv_id),
            lambda: trace_store.release_edit_lock(ws_id, conv_id),
        )

    def _worktree_manager(self) -> WorktreeManager:
        return WorktreeManager(self.config.trace_root / "worktrees")

    def _provision_workdir(self, scope: ProjectScope, conv_id: str) -> tuple[Path, TaskWorktree | None]:
        """Where this task's agents run. With more than one worker slot and a git
        project, that is a private worktree on its own branch (reused when the
        conversation already has one, e.g. after an approval); otherwise the project
        checkout itself, as before."""
        trace_store = self.approval_manager.trace_store
        recorded = trace_store.get_conversation_worktree(conv_id)
        if recorded and Path(recorded["path"]).is_dir():
            worktree = TaskWorktree(
                path=Path(recorded["path"]),
                branch=recorded["branch"],
                base_branch=recorded["base_branch"],
                base_commit=recorded["base_commit"],
            )
            return worktree.path, worktree
        if scope.max_workers <= 1 or not can_use_worktrees(scope.working_directory):
            # One worker, not a git repo, or a repo with no commit yet to branch from
            # (TWTradingStrategy): edit in place, one task at a time per checkout.
            return scope.working_directory, None
        manager = self._worktree_manager()
        worktree = manager.create(scope.working_directory, scope.name, conv_id)
        if scope.worktree_setup_command:
            try:
                manager.run_setup_command(
                    worktree, scope.working_directory, scope.worktree_setup_command, 600
                )
            except Exception:
                manager.remove(scope.working_directory, worktree)
                raise
        trace_store.record_conversation_worktree(
            conv_id,
            workspace_id=scope.workspace_id,
            path=str(worktree.path),
            branch=worktree.branch,
            base_branch=worktree.base_branch,
            base_commit=worktree.base_commit,
        )
        return worktree.path, worktree

    def _finalize_worktree(
        self, scope: ProjectScope, conv_id: str, worktree: TaskWorktree | None, status: str
    ) -> str:
        """After a run: keep whatever still needs a human or a later merge, drop the
        rest. Returns a note about unmerged work to append to the report ("" if none)."""
        if worktree is None:
            return ""
        manager = self._worktree_manager()
        repo = scope.working_directory
        trace_store = self.approval_manager.trace_store
        try:
            manager.commit_all(worktree, "harness: changes left in the worktree")
            ahead = manager.commits_ahead(repo, worktree)
            if ahead == 0 and status in {"succeeded", "failed"}:
                manager.remove(repo, worktree)
                trace_store.delete_conversation_worktree(conv_id)
                return ""
            if ahead > 0 and status in {"succeeded", "failed"}:
                return (
                    f"\n\n📂 變更在分支 {worktree.branch}（{worktree.path}），"
                    f"尚未合併進 {worktree.base_branch or '主工作目錄'}。"
                )
        except Exception:
            logger.exception(f"finalizing the worktree of {conv_id} failed")
        return ""

    def _knowledge_base_locations(self, scope: ProjectScope) -> str:
        """Where this project's knowledge lives in MemTrace — shared by the chat
        identity context and the Agent Loop's context items. Before 2026-10-01 only
        chat was told about the cold-memory workspace; the Agent Loop could search
        MemTrace but only knew the spec workspace from harness-scope.md, so every
        consolidated conversation note was effectively write-only for it."""
        memory_ws = self.config.memory_workspace_id_for(scope.name, scope.workspace_id)
        note_shape = (
            f"draft nodes titled \"Draft: {scope.name} <YYYY-MM-DD HH:MM>（turns #a–#b）· "
            "<how it opens>\" (content_type context, tags harness/draft/primary-session), "
            "each holding a batch of past chat turns and decisions, plus one nightly digest "
            f"per day titled \"{DIGEST_TITLE_PREFIX}: {scope.name} <YYYY-MM-DD>\" (tag "
            "daily-digest) with that day's decisions, facts and open items. A digest is "
            "linked by `extends` to the previous day's digest and by `extracted_from` to the "
            "drafts it was made from, so traverse from a digest to reach its raw evidence"
        )
        if memory_ws == scope.workspace_id:
            memory_line = (
                f"- Cold memory (what was discussed/decided before): no dedicated memory "
                f"workspace is configured, so the periodic background consolidation pass "
                f"writes into the spec workspace `{memory_ws}` itself, as {note_shape}."
            )
        else:
            memory_line = (
                f"- Cold memory (what was discussed/decided before): `{memory_ws}` "
                f"(\"Harness Memory\" in MemTrace), separate from the spec workspace. A "
                f"periodic background consolidation pass writes {note_shape}."
            )
        return "\n".join(
            [
                f"Knowledge-base locations for project '{scope.name}':",
                f"- Project specification (what should be built, accepted decisions): "
                f"`{scope.workspace_id}`.",
                memory_line,
                "The prior-discussion context is only a recent rolling window. When a "
                "past conversation, task, or decision you need isn't in it, search the "
                "cold-memory workspace with MemTrace's search_nodes (pass that "
                "workspace_id explicitly). Cold-memory notes are unreviewed draft "
                "evidence of what was said, not accepted spec: if one conflicts with "
                "the spec workspace, the spec wins and the conflict should be flagged.",
                "A local Claude Code project-memory directory (e.g. under "
                "~/.claude/projects/.../memory/) you may know of from your own tool "
                "context is unrelated to the Harness and is NOT one of this project's "
                "knowledge bases — only the workspaces named here are.",
            ]
        )

    def _identity_context(self, scope: ProjectScope) -> str:
        parts = [
            f"You are the Harness agent for project '{scope.name}' (workspace "
            f"`{scope.workspace_id}`). This is the project's harness-scope.md, its source of "
            f"identity, default risk level, and off-limits rules — do not ask the user to repeat "
            f"it:\n\n{scope.raw_markdown.strip()}",
            self._knowledge_base_locations(scope),
            "Language policy: every reply, recommendation, and summary sent back to the "
            "human — in every stage of this run — must be written in Traditional Chinese "
            "(繁體中文，台灣用語與正體字), never Simplified Chinese and never simplified "
            "phrasing/vocabulary, regardless of what language the human's own message used.",
        ]
        overviews = self._kb_indexes(scope)
        if overviews:
            parts.append(
                "Overviews the Controller keeps in the knowledge base — research directions and the "
                "record of its reviews of the harness's own work. They describe the past and a later "
                "one replaces them; when one says a problem existed, check whether it still does "
                "before telling the human it does:\n\n"
                + "\n\n".join(f"### {label}\n{body}" for label, body in overviews)[:MAX_OVERVIEW_CHARS]
            )
        digests = self.primary_session_mgr.get_recent_digests_context(scope.name)
        if digests:
            parts.append(f"{_DIGESTS_HEADER}\n\n{digests}")
        briefs = render_briefs_context(self.primary_session_mgr.trace_store, scope.name)
        if briefs:
            parts.append(f"{BRIEFS_HEADER}:\n\n{briefs}")
        rehydration = self.primary_session_mgr.get_rehydration_context(
            scope.name, tz=ZoneInfo(self.config.schedule_timezone)
        )
        if rehydration:
            parts.append(f"Prior discussion for this project (open items may still need action):\n\n{rehydration}")
        operator_profile = self._operator_profile_context(scope)
        if operator_profile:
            parts.append(
                "Operator preferences — the Harness itself derived these from this human's "
                "own past conversations and adopted them, so any of them may be wrong. Treat "
                "them as standing defaults; an explicit instruction in the current message "
                f"takes precedence:\n\n{operator_profile}\n\n"
                "如果使用者在這則訊息裡明確撤回、或更正上面其中一條偏好（例如「那個不用了」"
                "「改成…」），請在回覆最後另起一行輸出 "
                f"`{PREFERENCE_CORRECT_MARKER}<編號>::retire`（撤回），或 "
                f"`{PREFERENCE_CORRECT_MARKER}<編號>::replace::<更正後的一句話>`（更正）；"
                "編號是上面方括號裡的數字，同一則回覆可以有多行。只有使用者明確針對那一條偏好"
                "表態時才輸出；一般討論、單次的要求、對專案內容的意見都不算更正。"
            )
        role_summary = self._role_profiles_summary(scope)
        if role_summary:
            parts.append(
                "Agent Loop role -> provider/model configuration for this project (this "
                "IS the authoritative, current answer to \"which model does each stage "
                "use\" — don't guess, don't offer to go query MemTrace for this, it isn't "
                "stored there):\n\n" + role_summary
            )
        return "\n\n---\n\n".join(parts)

    def _role_profiles_summary(self, scope: ProjectScope) -> str:
        """So a chat question like "what models does Controller/Developer use" can be
        answered from what the Harness actually has locally, instead of the model
        guessing or conflating it with an unrelated MemTrace workspace mentioned in
        harness-scope.md. Best-effort: a broken profiles file must not break chat."""
        try:
            profiles = load_role_profiles(self.config.role_profiles_file_for(scope.name))
        except Exception:
            logger.exception(f"Failed to load role profiles for '{scope.name}'; omitting from context")
            return ""
        lines = []
        for profile_id, profile in profiles.items():
            entry = f"- {profile_id}: {profile.provider}/{profile.model} ({profile.permission})"
            if profile.fallbacks:
                fb = ", ".join(f"{f.provider}/{f.model}" for f in profile.fallbacks)
                entry += f" — fallback: {fb}"
            lines.append(entry)
        return "\n".join(lines)

    def _operator_profile_context(self, scope: ProjectScope) -> str:
        """Adopted preference rules (memory_digest.py), global ones plus this project's
        own, read from local SQLite, each with its id so a correction can name it. The
        Harness adopts them itself from the nightly digest and the human corrects them in
        chat or on the status page. Until 2026-10-01 this read a MemTrace node that an
        automatic classifier appended raw turns to, with no evidence check, and a whole
        stock-analysis reply ended up injected into every chat as an instruction."""
        try:
            return preference_context_for(
                self.primary_session_mgr.trace_store, scope.name, with_ids=True
            )
        except Exception:
            logger.exception("Failed to load adopted operator preferences; continuing without them")
            return ""

    def _kb_index_rows(self, scope: ProjectScope) -> list[tuple[str, str, str]]:
        """The overviews the Controller keeps for this project: (workspace, kind, text), from the
        specification workspace (research directions) and the memory workspace (record of its work
        reviews). Read from the local copy kept when they were written."""
        rows: list[tuple[str, str, str]] = []
        for workspace_id, _ in self._charter_workspaces(scope):
            for index in self.approval_manager.trace_store.list_workspace_indexes(workspace_id):
                rows.append((workspace_id, index["kind"], index["body"]))
        return rows

    def _kb_indexes(self, scope: ProjectScope) -> list[tuple[str, str]]:
        labels = {"directions": "研究與策略總覽", "work-review": "工作復盤紀錄"}
        return [(labels.get(kind, kind), body) for _, kind, body in self._kb_index_rows(scope)]

    def _charter_workspaces(self, scope: ProjectScope) -> list[tuple[str, str]]:
        memory_ws = self.config.memory_workspace_id_for(scope.name, scope.workspace_id)
        pairs = [(scope.workspace_id, "spec")]
        if memory_ws != scope.workspace_id:
            pairs.append((memory_ws, "cold-memory"))
        return pairs

    def _precedent_context(self, scope: ProjectScope) -> str:
        """The Controller's view of how this operator has decided before (past decisions plus
        adopted preferences). Best effort: without it the Controller simply decides as it did
        before this existed."""
        try:
            return precedent_block(
                self.primary_session_mgr.trace_store,
                scope.name,
                preferences=self._operator_profile_context(scope),
            )
        except Exception:
            logger.exception("Failed to build operator precedent; continuing without it")
            return ""

    def _project_context_items(self, scope: ProjectScope) -> list[ContextItem]:
        """What the Agent Loop actually needs to do its job, carried through the
        TaskEnvelope's structured context_items (respecting each role's
        context_policy — see adapter_factory.py/loop.py's per-stage filtering) instead
        of concatenated into `goal` text. Deliberately excludes the operator
        preference profile and the role->model summary: those describe how *Harness*
        should talk to the human (quick-chat replies, approval messages), not
        information Controller/Planner/Red Team/Developer need to do technical work —
        see _identity_context(), still used as-is for _chat_reply_and_maybe_start_task(). Off-limits
        rules travel via TaskEnvelope.constraints (scope.off_limits), not here.

        Fixes the same bug class as the resume_goal-augmentation-nesting fix, at the
        root instead of patching around it: since `goal` no longer carries any of
        this, there is nothing for a resumed conversation to re-wrap or duplicate."""
        items = [
            ContextItem(
                ref=f"harness:project-scope:{scope.name}",
                title="Project scope",
                body=scope.raw_markdown.strip(),
                content_type="context",
                source="harness",
            ),
            ContextItem(
                ref=f"harness:knowledge-bases:{scope.name}",
                title="Knowledge-base locations",
                body=self._knowledge_base_locations(scope),
                content_type="context",
                source="harness",
            ),
        ]
        for workspace_id, label in self._charter_workspaces(scope):
            charter = self.approval_manager.trace_store.get_workspace_charter(workspace_id)
            if charter:
                items.append(
                    ContextItem(
                        ref=f"harness:kb-map:{workspace_id}",
                        title=f"Knowledge-base map: {label} workspace {workspace_id}",
                        body=(
                            f"The Controller's own map of workspace `{workspace_id}` ({label}) — read "
                            f"it before searching that workspace:\n\n{charter['body']}"
                        ),
                        content_type="context",
                        source="harness",
                    )
                )
        for workspace_id, kind, body in self._kb_index_rows(scope):
            items.append(
                ContextItem(
                    ref=f"harness:kb-index:{workspace_id}:{kind}",
                    title=f"Overview kept by the Controller ({kind}) in workspace {workspace_id}",
                    body=body[:MAX_OVERVIEW_CHARS],
                    content_type="context",
                    source="harness",
                )
            )
        digests = self.primary_session_mgr.get_recent_digests_context(scope.name)
        if digests:
            items.append(
                ContextItem(
                    ref=f"harness:daily-digests:{scope.name}",
                    title="Recent daily digests",
                    body=f"{_DIGESTS_HEADER}\n\n{digests}",
                    content_type="context",
                    source="harness",
                )
            )
        precedent = self._precedent_context(scope)
        if precedent:
            items.append(
                ContextItem(
                    ref=f"harness:operator-precedent:{scope.name}",
                    title="Operator precedent (past decisions and standing preferences)",
                    body=precedent,
                    content_type=PRECEDENT_CONTENT_TYPE,
                    source="harness",
                )
            )
        rehydration = self.primary_session_mgr.get_rehydration_context(
            scope.name, tz=ZoneInfo(self.config.schedule_timezone)
        )
        if rehydration:
            items.append(
                ContextItem(
                    ref=f"harness:prior-discussion:{scope.name}",
                    title="Prior discussion (open items may still need action)",
                    body=rehydration,
                    content_type="context",
                    source="harness",
                )
            )
        return items

    def _report_run_outcome(
        self, *, conv_id: str, summary, final_text: str, chat_id: int | None = None
    ) -> None:
        """Report a finished (or checkpoint-stopped) Agent Loop run. A 'needs_human'
        status can mean a fresh ApprovalRequest is now pending (e.g. the git-push
        checkpoint in loop.py) — surface that with its own tappable Approve/Reject
        buttons instead of only a passive status line the human would otherwise have
        to go find themselves via /status or a blind reply."""
        if summary.status == "needs_human":
            pending = self.approval_manager.get_pending_for_conversation(conv_id)
            if pending is not None:
                self.notify_approval_request(pending)
                return
        claim = self._file_completion_claim(conv_id, summary)
        if claim is not None:
            claim_id, extra_lines = claim
            text = "\n".join([final_text, *extra_lines]) + "\n\n請驗收：完成了就按「驗收通過」，不是你要的就按「還沒完成」（沒有期限，沒按就一直維持待驗收）。"
            for cid in [chat_id] if chat_id is not None else sorted(self.allowed_chat_ids):
                self.send_message_with_keyboard(cid, text, claim_keyboard(claim_id))
            return
        if chat_id is not None:
            self.send_message(chat_id, final_text)
        else:
            self.notify_all_allowlisted(final_text)

    def notify_garden_outcome(self, outcome: GardenOutcome, chat_id: int | None = None) -> None:
        """Tell the operator what a tidying pass did, with a one-tap undo when it deleted
        anything (the nodes are in MemTrace's trash and copied locally)."""
        text = describe_outcome(outcome)
        keyboard = (
            [[{"text": "↩️ 還原這批刪除", "callback_data": f"kb_undo:{outcome.run_id}"}]]
            if outcome.deleted
            else None
        )
        for cid in [chat_id] if chat_id is not None else sorted(self.allowed_chat_ids):
            if keyboard:
                self.send_message_with_keyboard(cid, text, keyboard)
            else:
                self.send_message(cid, text)
        self.primary_session_mgr.record_turn(
            project=outcome.project, speaker="assistant", turn_type="chat", content=text
        )

    def _start_work_review(self, scope: ProjectScope | None, chat_id: int) -> str:
        """Start a review of the harness's own work for a project, in the background. Shared by
        the /review command and the chat model's request. Returns what to tell the operator."""
        if scope is None:
            return "請指定專案，例如 /review TWTradingStrategy。"
        if self.work_review_now is None:
            return "工作復盤在這個 gateway 沒有啟用。"
        self.work_review_now(scope, chat_id)
        return f"🔍 開始復盤「{scope.name}」自上次復盤以來的工作，完成後會回報（可能要一兩分鐘）。"

    def notify_review_outcome(self, outcome: ReviewOutcome, scope: ProjectScope, chat_id: int | None = None) -> None:
        """Report a work review: the findings, then each proposed change as a decision card the
        operator answers (the answer is kept as precedent; the harness changes nothing by itself)."""
        text = describe_review(outcome)
        keyboard = (
            [[{"text": "↩️ 撤銷這次寫入的教訓", "callback_data": f"kb_undo:{outcome.lesson_run_id}"}]]
            if outcome.lesson_run_id is not None and outcome.lessons
            else None
        )
        targets = [chat_id] if chat_id is not None else sorted(self.allowed_chat_ids)
        for cid in targets:
            if keyboard:
                self.send_message_with_keyboard(cid, text, keyboard)
            else:
                self.send_message(cid, text)
        self.primary_session_mgr.record_turn(
            project=outcome.project, speaker="assistant", turn_type="chat", content=text
        )
        for index, proposal in enumerate(outcome.proposals, start=1):
            req = self.approval_manager.request_approval(
                conversation_id=f"review_{outcome.review_id}_{index}",
                workspace=outcome.workspace_id,
                working_directory=str(scope.working_directory),
                reason=PROPOSAL_REASON,
                proposed_action=f"{proposal['title']}\n{proposal['card']['situation']}",
                decision_card=proposal["card"],
            )
            self.notify_approval_request(req)

    def notify_promotion_outcome(self, outcome: PromotionOutcome, chat_id: int | None = None) -> None:
        """Tell the operator what a promotion pass wrote, with an undo for what it created."""
        text = describe_promotion(outcome)
        keyboard = (
            [[{"text": "↩️ 撤銷這批新增", "callback_data": f"kb_undo:{outcome.run_id}"}]]
            if outcome.created and outcome.run_id is not None
            else None
        )
        for cid in [chat_id] if chat_id is not None else sorted(self.allowed_chat_ids):
            if keyboard:
                self.send_message_with_keyboard(cid, text, keyboard)
            else:
                self.send_message(cid, text)
        self.primary_session_mgr.record_turn(
            project=outcome.project, speaker="assistant", turn_type="chat", content=text
        )

    def _resolve_garden_undo(self, run_id_text: str, chat_id: int, message_id: int | None) -> str:
        if self.memtrace_client is None or not run_id_text.isdigit():
            return "無法還原"
        trace_store = self.approval_manager.trace_store
        restored, recreated, failed = undo_gardening_run(self.memtrace_client, trace_store, int(run_id_text))
        removed, remove_failed = undo_created_nodes(self.memtrace_client, trace_store, int(run_id_text))
        failed += remove_failed
        if message_id is not None:
            self.clear_message_keyboard(chat_id, message_id)
        if removed and not (restored or recreated):
            text = f"↩️ 已撤銷 {removed} 個新增的節點（進了垃圾桶，30 天內可由 MemTrace 還原）"
            if failed:
                text += f"；{failed} 個撤銷失敗，請看日誌"
            self.send_message(chat_id, text + "。")
            return "已撤銷" if not failed else "部分撤銷"
        text = f"↩️ 已還原 {restored + recreated} 個節點"
        if recreated:
            text += f"（其中 {recreated} 個已不在垃圾桶，用本機留的內容重建）"
        if failed:
            text += f"；{failed} 個還原失敗，請看日誌"
        self.send_message(chat_id, text + "。")
        return "已還原" if not failed else "部分還原"

    def _file_completion_claim(self, conv_id: str, summary) -> tuple[str, list[str]] | None:
        """At the end of a governed run the Controller's converge stage declares the work done:
        record that as a claim awaiting the operator's acceptance, and apply the knowledge-base
        updates it proposed (completion_claims.py). None when this run is not such a completion
        (a failed or stopped run, a schedule or operational run that never reached converge).
        Never raises: the result is reported either way."""
        try:
            if summary.status != "succeeded":
                return None
            converge = next(
                (
                    s for s in reversed(summary.stages)
                    if s.stage == "converge" and (s.artifact or {}).get("action") in {"finish", "merge"}
                ),
                None,
            )
            scope = next((p for p in self.projects if p.workspace_id == summary.task.workspace_id), None)
            if converge is None or scope is None:
                return None
            trace_store = self.approval_manager.trace_store
            claim_id = trace_store.create_completion_claim(
                project=scope.name,
                conversation_id=summary.conversation_id or conv_id,
                summary=summary.recommendation,
                workspace_id=summary.task.workspace_id,
                evidence={
                    "stages": [f"{s.stage}:{s.state}" for s in summary.stages],
                    "controller_reason": (converge.artifact or {}).get("reason"),
                },
            )
            lines: list[str] = []
            ops = kb_updates_from_artifact(converge.artifact)
            if ops and self.memtrace_client is not None:
                applied = apply_kb_updates(
                    self.memtrace_client,
                    workspace_id=summary.task.workspace_id,
                    ops=ops,
                    claim_id=claim_id,
                    conversation_id=summary.conversation_id or conv_id,
                    claim_summary=summary.recommendation[:500],
                )
                trace_store.attach_claim_nodes(
                    claim_id, node_id=applied["node_id"], claim_node_id=applied["claim_node_id"]
                )
                lines = [f"🧠 {line}" for line in applied["lines"]]
            return claim_id, lines
        except Exception:
            logger.exception(f"filing the completion claim for {conv_id} failed; reporting without it")
            return None

    def _resolve_claim(self, action: str, claim_id: str, chat_id: int, message_id: int | None) -> str:
        """The operator's answer to a completion claim. Accepting moves the claimed node to
        done; sending it back reopens it and is kept as the decision the Controller learns from
        (listed, marked, in the precedent it reads next)."""
        trace_store = self.approval_manager.trace_store
        claim = trace_store.get_completion_claim(claim_id)
        if claim is None:
            return f"找不到驗收項目 {claim_id}"
        accepted = action == CLAIM_OK
        if not trace_store.resolve_completion_claim(claim_id, "accepted" if accepted else "rejected"):
            return "這項已經處理過了"
        problem = None
        if self.memtrace_client is not None:
            problem = (accept_claim if accepted else reject_claim)(self.memtrace_client, claim)
        choice = "驗收通過" if accepted else "還沒完成"
        record_id = trace_store.add_decision_record(
            project=claim["project"],
            kind="task_claim",
            situation=claim["summary"],
            outcome="claim_accepted" if accepted else "claim_rejected",
            chosen_text=choice,
            followed=accepted,
            subject=claim_id,
            conversation_id=claim["conversation_id"],
        )
        self.primary_session_mgr.record_turn(
            project=claim["project"],
            speaker="user",
            turn_type="decision",
            content=decision_turn_text(
                record_id=record_id,
                kind="task_claim",
                situation=claim["summary"],
                outcome="claim_accepted" if accepted else "claim_rejected",
                chosen_text=choice,
                recommended_label=None,
            ),
        )
        if message_id is not None:
            self.clear_message_keyboard(chat_id, message_id)
        if accepted:
            note = f"（知識庫更新有問題：{problem}）" if problem else ""
            self.send_message(chat_id, f"✅ 已驗收 {claim_id}。{note}")
            return "已驗收"
        note = f"（知識庫更新有問題：{problem}）" if problem else ""
        self.send_message(
            chat_id,
            f"↩️ 已退回 {claim_id}，相關節點重新標為未完成，這次的宣告也記下來了，之後的判斷會參考。"
            f"哪裡沒完成可以直接回覆這則訊息告訴我。{note}",
        )
        return "已退回"

    @staticmethod
    def _model_summary(summary) -> str:
        """One line per stage: which provider/model actually answered, including
        when a stage fell back off its primary (e.g. Codex quota-exhausted -> Gemini)."""
        parts = []
        for stage in summary.stages:
            execution = stage.response.execution
            model = execution.resolved_model or execution.requested_model or "?"
            entry = f"{stage.profile_id}={execution.provider}/{model}"
            if stage.fallback_from_model:
                entry += f"（fallback，原本 {stage.fallback_from_model}）"
            parts.append(entry)
        return "、".join(parts) if parts else "無"

    _TASK_START_MARKER = "HARNESS_TASK_START::"
    _REVIEW_START_MARKER = "HARNESS_REVIEW_START::"
    _SCHEDULE_START_MARKER = "HARNESS_SCHEDULE_START::"
    _SCHEDULE_PAUSE_MARKER = "HARNESS_SCHEDULE_PAUSE::"
    _SCHEDULE_RESUME_MARKER = "HARNESS_SCHEDULE_RESUME::"
    _SCHEDULE_CANCEL_MARKER = "HARNESS_SCHEDULE_CANCEL::"
    _PAUSE_INDEFINITELY = datetime(9999, 12, 31, tzinfo=timezone.utc)

    _CHAT_MEMTRACE_READ_TOOLS = (
        "mcp__memtrace__search_nodes,mcp__memtrace__get_node,"
        "mcp__memtrace__list_nodes,mcp__memtrace__traverse"
    )

    @staticmethod
    def _memtrace_notice() -> str:
        return (
            "你代表 Harness，對這個專案的 MemTrace 知識庫有讀與寫的能力：讀用 MCP 工具 `memtrace`"
            "（search_nodes、get_node、list_nodes、traverse）；寫則是你在回覆中另起一行輸出"
            f"「{CHAT_NOTE_MARKER}<factual|procedural|context|inquiry|preference>::<標題>::<一行內容>」，"
            "Harness 會驗證並替你寫入（每則回覆最多 3 行，內容寫成單行）。使用者要你記錄、或對話得出值得"
            "保存的決定／事實時就這樣寫，寫入結果 Harness 會附在回覆後面，你不要自己宣稱已存入。另外，"
            "每則對話本來就會由 Harness 定期整理成草稿存進知識庫，不是「沒有存」。如果你發現 MemTrace 讀取工具"
            "實際上不能用，要明說是工具故障，並用任務啟動標記開一個診斷任務，不要只叫使用者去找人。\n\n"
        )

    @staticmethod
    def _ops_notice(scope: ProjectScope) -> str:
        """The chat sandbox has no network, so a failure seen from it says nothing about the
        outside. Say so, and point at the operations tools (ops_mcp.py) that look from outside."""
        notice = (
            "你的沙盒沒有網路：在這裡連不上、DNS 解析失敗，都不能當作外部服務（例如證交所 TWSE）真的壞了"
            "或本機被擋的證據，絕對不要拿來回答使用者。\n"
        )
        if not ops_server_spec(scope, None):
            return notice + (
                "這個專案沒有設定診斷工具，查不到真相時就如實說「我在這裡驗證不了」，並說明要怎麼驗證。\n\n"
            )
        return notice + (
            "使用者問某個排程、腳本或外部服務為什麼失敗、現在連不連得上時，先用 MCP 工具 `ops` 自己查，"
            "再回答，不要要使用者等或叫他去查：`http_probe`（從外面實際發請求，回報狀態碼、耗時、大小、"
            "錯誤；attempts 可重複測幾次看耗時是否穩定）、`read_log`（看失敗的 log，名稱用 list_logs）、"
            "`list_jobs`／`run_job`（要把失敗的工作重跑時，run_job 只是提出要求，使用者要在 Telegram 按"
            "「確認執行」才會跑；你要說「已請你確認」，不要說已經跑了）。回答要寫出量測到的事實，並判斷"
            "是服務端的問題，還是專案自己的程式（逾時太短、舊的端點、被限流）。確定原因在專案程式、需要"
            "改程式時，才用上面的任務啟動標記交給開發流程；只是診斷或重跑，不要開任務。\n\n"
        )

    @staticmethod
    def _taiwantrade_notice(project: str | None = None) -> str:
        """Tell the chat model it has the read-only TaiwanTrade tools. Without this it
        tries `curl 127.0.0.1:8000` from its network-less sandbox, fails, and reports a
        connection error even though the proxy tools were attached and working."""
        if not mcp_server_spec():
            return ""
        notice = (
            "你有唯讀的 MCP 工具 `taiwantrade`（get_balance 銀行餘額、get_positions 庫存持股、"
            "get_quotes 即時報價、get_stock_daily、list_orders、get_settlements 等）。使用者要查"
            "餘額、持股、報價或委託時，請直接呼叫這些工具；不要用 shell/curl 連 127.0.0.1:8000——"
            "你的沙盒沒有網路，那樣一定會失敗，而且你不需要也不應該讀取任何金鑰檔。工具如果回傳"
            "錯誤，就如實轉述那個錯誤訊息。\n"
        )
        if project and project.lower() in order_projects():
            notice += (
                "下單：你可以用 `create_order_intent` 提出委託（限價單；盤中零股要設 is_odd_lot=true，"
                "數量單位是股）。它不會真的下單——只會建立一筆待確認的委託，系統會在 Telegram 給使用者"
                "「確認／取消」按鈕，使用者按確認後才會送到券商，結果也會由系統自動回報給使用者。"
                "所以：使用者要下單時，先確認股票、買賣方向、數量、價格都清楚（缺價格就問，不要自己"
                "猜；使用者說「開盤價」但還沒有開盤價時，如實說還沒有，請他給價格），再呼叫工具，然後"
                "告訴使用者「已提出委託，請到 Telegram 按確認」。不要說已經下單，也不要叫使用者去券商"
                "App 自己下。\n"
                "撤單：用 `request_order_cancel`（先用 list_orders 找到委託編號），一樣只是提出撤單，"
                "使用者在 Telegram 按「確認撤單」後才會送出。注意：系統記錄的委託狀態可能落後券商"
                "（已成交的單可能還顯示 PendingSubmit），判斷有沒有成交要以 get_positions 為準；已成交的單"
                "撤不了，如實告訴使用者。你不能改單。\n"
            )
        else:
            notice += "這些工具只能查詢，不能下單、改單或撤單；要交易請告訴使用者自己操作。\n"
        return notice + "\n"

    _APPROVAL_ANSWER_MARKER = "HARNESS_APPROVAL_ANSWER::"

    @classmethod
    def _pending_approval_notice(cls, pending: ApprovalRequestData | None) -> str:
        """Tell the chat model the message it answers is a paused task's question, and when
        (only then) to resume it."""
        if pending is None:
            return ""
        return (
            f"使用者這則訊息是滑動回覆一則「待回答的問題」[{pending.id}]（一個暫停中的任務在等這個答案，"
            f"原因：{pending.reason}，內容見上面被回覆的訊息）。請判斷使用者是在**回答**它，還是別的事：\n"
            "- 如果使用者提供了它要的資訊、決定或指示（即使很簡短，例如「照這個做」「用方案 B」「改成 3%」），"
            f"就在回覆的最後另起一行輸出「{cls._APPROVAL_ANSWER_MARKER}<使用者的答案，保留他的原意與關鍵細節>」，"
            "任務就會帶著這個答案繼續執行。\n"
            "- 如果使用者是在**問你問題**、要你解釋或查證、閒聊、或你不確定他是不是在回答，就**不要**輸出這一行，"
            "只好好回答他；任務會繼續暫停，等他明確回答或按按鈕。不確定時寧可不輸出——多問一句的代價，"
            "遠小於誤啟動一輪完整流程。\n"
            "不要在同一則回覆同時輸出這一行和任務啟動標記。\n\n"
        )

    def _chat_reply_and_maybe_start_task(
        self,
        scope: ProjectScope,
        chat_id: int,
        text: str,
        quoted_text: str | None = None,
        pending_approval: ApprovalRequestData | None = None,
    ) -> str:
        """The entire chat front door goes through one model call now: no more
        separate CHAT/QUESTION/LOOKUP/TASK classification pass, and no more
        "!"/"/task" marker to mechanically queue a governed task (2026-09-10,
        explicit user request). The model gets full project identity/context and
        is told that if — and only if — this conversation makes it confident the
        human wants real development work started now, it should end its reply
        with a line "HARNESS_TASK_START::<goal>"; the harness strips that line
        out of what the human sees and uses it to kick off the governed
        Controller/Planner/RedTeam/Developer loop in the background, same as the
        old "!" trigger did — except the decision now comes from the model
        judging the conversation, not a mechanical prefix, and there is no
        separate approval step in between: reaching this marker together with
        the human in chat IS the decision to proceed.

        off_limits is enforced the same way (2026-09-17): chat_triage.py no longer
        keyword-blocks a message mentioning an off_limits term before it reaches the
        model — that mechanical check couldn't tell "the key is in .env, use it"
        (a safe reference) from an actual leaked value, and in practice only ever
        caught the safe case. The prompt below spells out the project's off_limits
        list explicitly and tells the model to treat it as a real boundary while
        replying, not just background text to ignore."""
        from memtrace_harness.cli_process import CliProcessRunner

        candidates = self.config.chat_candidates_for(scope.name)
        if not candidates:
            msg = "（對話尚未設定：HARNESS_CHAT_PROVIDER 沒有值)"
            self.send_message(chat_id, msg)
            self.primary_session_mgr.record_turn(
                project=scope.name, speaker="assistant", turn_type="chat", content=msg
            )
            return msg

        off_limits_notice = ""
        if scope.off_limits:
            rules = "、".join(scope.off_limits)
            off_limits_notice = (
                f"這個專案的禁區規則（off_limits）：{rules}。這些是你在這個對話裡要遵守的邊界——"
                "不要執行、印出、或協助繞過這些規則所指的實際內容（例如印出金鑰/憑證的真實值、"
                "操作正式環境資料庫）。但單純聊到這些字眼本身、或使用者只是告訴你金鑰放在哪個"
                "檔案/變數名稱（沒有貼出實際值），是安全且被允許的，不要因為訊息裡出現這些字就"
                "拒絕回覆或裝作沒看到。\n\n"
            )

        prompt = (
            f"{self._identity_context(scope)}\n\n---\n\n"
            f"{off_limits_notice}"
            "你正在跟這個專案的負責人自由對話，直接回覆使用者的訊息即可，一律使用繁體中文。\n\n"
            "你可以直接使用 MemTrace 的唯讀查詢工具（search_nodes、get_node、list_nodes、"
            f"traverse）查這個專案的知識庫，工作區 id 是 {scope.workspace_id}；使用者要你查知識庫時"
            "就直接查，不需要先徵求任何核准。不要說「需要核准權限」或「正在等待權限確認」——沒有"
            "這樣的核准流程。如果查詢真的失敗或被拒絕，就如實說「這次知識庫查詢失敗」以及看得到的"
            "原因，不要假裝在等待。\n\n"
            "如果，而且只有在，根據這則訊息（以及前面的對話），你確信使用者現在真的是要你"
            "動手進行開發——修改這個 repo 的程式碼、實際落地某個具體改動——才在回覆的最後"
            f"另起一行，格式為「{self._TASK_START_MARKER}<一句話描述要做的具體任務>」，交給"
            "受控的 Controller/Planner/RedTeam/Developer 流程去執行。單純聊天、討論、還在"
            "釐清需求、或只是在問問題時，絕對不要輸出這一行——不確定就不要輸出。\n\n"
            "如果，而且只有在，使用者明確要你之後每隔一段時間、或每天/每個工作日固定時間"
            "重複執行某件事——才在回覆的最後另起一行，格式為「"
            f"{self._SCHEDULE_START_MARKER}<kind>::<spec>::<一句話描述要做的具體任務>」，"
            "其中 kind 是 interval、daily 或 weekdays 三選一：interval 的 spec 是週期秒數"
            "（例如 3600 代表每小時一次，最少 60 秒）；任務只在特定時段或只在工作日才需要"
            "（例如台股盤中 09:00-13:30、只在交易日）時，一定要把限制寫進 spec，例如"
            "「600@09:00-13:30@weekdays」，不要只寫在任務描述裡——spec 之外的限制系統不會執行，"
            "排程會在收盤後、夜裡、週末照樣每個週期跑一次；daily 的 spec 是每天固定時間，"
            "24 小時制 HH:MM（例如 09:00）；weekdays 的 spec 跟 daily 一樣但只在週一到"
            "週五觸發。同一則回覆不要同時輸出這一行和上面的任務啟動標記。不確定使用者是否"
            "真的要排程、或排程細節（週期、時間）還沒問清楚時，絕對不要輸出這一行，先在對話"
            "裡把細節問清楚。\n\n"
            "如果，而且只有在，使用者要你復盤 harness 自己的工作——回顧最近的任務與排程做得如何、"
            "哪裡卡住或失敗、要怎麼改進（不是要你檢討交易損益，也不是要你開發某個功能）——才在回覆"
            f"最後另起一行，格式為「{self._REVIEW_START_MARKER}<一句話說明想看什麼>」。系統會自動產生"
            "有數據的復盤報告，你不需要自己整理數字。同一則回覆不要同時輸出任務啟動標記。\n\n"
            f"{self._memtrace_notice()}"
            f"{self._ops_notice(scope)}"
            f"{self._schedule_control_notice(scope)}"
            f"{self._taiwantrade_notice(scope.name)}"
            f"{self._quoted_reply_notice(quoted_text)}"
            f"{self._pending_approval_notice(pending_approval)}"
            f"使用者訊息：{text}"
        )
        failures: list[str] = []
        for provider, model in candidates:
            # Headless `claude --print` has nobody to approve a tool prompt, so an
            # un-allowlisted MCP call is just refused (and the model then made up a
            # "waiting for approval" story — 2026-09-30 貿聯 lookup). MemTrace's
            # read-only lookups are safe to pre-allow; writes stay unavailable.
            cmd = self.config.chat_command(
                provider, model, prompt,
                claude_allowed_tools=self._CHAT_MEMTRACE_READ_TOOLS,
                taiwantrade=True,
                order_project=scope.name,
                ops_server=ops_server_spec(scope, self.config.trace_db_path),
                memtrace_read=True,
            )
            result = CliProcessRunner().run(cmd, cwd=scope.working_directory, timeout_seconds=60)
            if result.return_code == 0 and result.stdout.strip():
                if failures:
                    logger.warning(
                        f"chat reply fell back to {provider}/{model} for project "
                        f"'{scope.name}' after: {'; '.join(failures)}"
                    )
                # Preference corrections first, and only their own lines are removed, so a
                # task or schedule marker on another line still reaches the extractors.
                stdout, corrections = self._extract_preference_corrections(result.stdout.strip())
                stdout, note_payloads = self._extract_chat_notes(stdout)
                confirmations = self._apply_preference_corrections(scope, corrections, text)
                confirmations += self._apply_chat_notes(scope, note_payloads)
                reply_text, goal = self._extract_task_start(stdout)
                reply_text, schedule_directive = self._extract_schedule_start(reply_text)
                reply_text, schedule_controls = self._extract_schedule_controls(reply_text)
                reply_text, approval_answer = self._extract_marker_line(
                    reply_text, self._APPROVAL_ANSWER_MARKER
                )
                reply_text, review_request = self._extract_marker_line(reply_text, self._REVIEW_START_MARKER)
                if confirmations:
                    reply_text = "\n".join(filter(None, [reply_text, "", *confirmations])).strip()
                attribution = f"🧩 {provider}/{model or '預設模型'}"
                reply = f"{reply_text}\n\n{attribution}" if reply_text else attribution
                self.primary_session_mgr.record_turn(
                    project=scope.name, speaker="assistant", turn_type="chat", content=reply_text or reply
                )
                self.send_message(chat_id, reply)
                if approval_answer and pending_approval is not None and not goal:
                    # Only honored for the approval this very message replied to, and only
                    # while it is still pending (a double reply must not resume twice).
                    current = self.approval_manager.get_request(pending_approval.id)
                    if current is not None and current.status == "pending":
                        self._resolve_approval_action(current.id, "clarify", chat_id, approval_answer)
                if goal:
                    self.primary_session_mgr.record_turn(
                        project=scope.name, speaker="user", turn_type="decision", content=goal
                    )
                    self._start_or_queue_task(scope, goal, chat_id)
                if review_request and not goal:
                    self.send_message(chat_id, self._start_work_review(scope, chat_id))
                if schedule_directive:
                    self._create_schedule(scope, chat_id, schedule_directive)
                for action, payload in schedule_controls:
                    self._apply_schedule_control(scope, chat_id, action, payload)
                return reply
            detail = result.error or result.stderr.strip() or f"exit code {result.return_code}"
            failures.append(f"{provider}/{model or '預設模型'}：{detail}")

        joined = "；".join(failures)
        msg = f"（回覆失敗：{joined}）"
        self.send_message(chat_id, msg)
        self.primary_session_mgr.record_turn(
            project=scope.name, speaker="assistant", turn_type="chat", content=msg
        )
        return msg

    @classmethod
    def _extract_marker_line(cls, raw: str, marker: str) -> tuple[str, str | None]:
        """Split a model reply into (human-visible text, marker payload or None) by
        pulling out a trailing "<marker><payload>" line if present, scanning from the
        bottom so the marker line doesn't have to be the very last line of output."""
        lines = raw.splitlines()
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith(marker):
                payload = stripped[len(marker):].strip()
                reply_text = "\n".join(lines[:i]).rstrip()
                return reply_text, (payload or None)
        return raw, None

    @staticmethod
    def _extract_chat_notes(raw: str) -> tuple[str, list[str]]:
        """Pull every HARNESS_KB_NOTE:: line out of a chat reply, wherever it sits."""
        kept, payloads = [], []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(CHAT_NOTE_MARKER):
                payloads.append(stripped[len(CHAT_NOTE_MARKER):].strip())
            else:
                kept.append(line)
        return "\n".join(kept).rstrip(), payloads

    def _apply_chat_notes(self, scope: ProjectScope, payloads: list[str]) -> list[str]:
        """Write what the chat model asked to remember into this project's memory workspace.
        Failures are reported to the operator, never swallowed: the model must not be left
        believing something was saved."""
        ops = chat_notes_to_ops(payloads)
        if not payloads:
            return []
        if not ops:
            return ["🧠 知識庫沒有寫入：記錄格式不正確。"]
        if self.memtrace_client is None:
            return ["🧠 知識庫沒有寫入：這個 Harness 沒有連上 MemTrace。"]
        applied = apply_kb_updates(
            self.memtrace_client,
            workspace_id=self.config.memory_workspace_id_for(scope.name, scope.workspace_id),
            ops=ops,
            claim_id="chat",
            conversation_id=f"chat_{scope.name}",
            claim_summary="",
            origin="聊天",
            note_tags=CHAT_NOTE_TAGS,
        )
        return [f"🧠 {line}" for line in applied["lines"]]

    @staticmethod
    def _extract_preference_corrections(raw: str) -> tuple[str, list[str]]:
        """Pull every "HARNESS_PREFERENCE_CORRECT::<payload>" line out of a model reply,
        wherever it sits, leaving all other lines untouched."""
        kept, payloads = [], []
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(PREFERENCE_CORRECT_MARKER):
                payloads.append(stripped[len(PREFERENCE_CORRECT_MARKER):].strip())
            else:
                kept.append(line)
        return "\n".join(kept).rstrip(), payloads

    def _apply_preference_corrections(
        self, scope: ProjectScope, payloads: list[str], user_text: str
    ) -> list[str]:
        """Best-effort: the model decided the human corrected a preference; a malformed
        or stale directive is dropped rather than failing the reply."""
        confirmations = []
        today = datetime.now(ZoneInfo(self.config.schedule_timezone)).date().isoformat()
        for payload in payloads:
            try:
                done = apply_chat_preference_correction(
                    self.primary_session_mgr.trace_store, scope.name, payload, user_text, today
                )
            except Exception:
                logger.exception(f"applying preference correction {payload!r} failed; ignoring it")
                continue
            if done:
                confirmations.append(done)
        return confirmations

    @classmethod
    def _extract_task_start(cls, raw: str) -> tuple[str, str | None]:
        """Pull a trailing "HARNESS_TASK_START::<goal>" line out of a model reply."""
        return cls._extract_marker_line(raw, cls._TASK_START_MARKER)

    @classmethod
    def _extract_schedule_start(cls, raw: str) -> tuple[str, str | None]:
        """Pull a trailing "HARNESS_SCHEDULE_START::<kind>::<spec>::<goal>" line out of
        a model reply."""
        return cls._extract_marker_line(raw, cls._SCHEDULE_START_MARKER)

    def _schedule_control_notice(self, scope: ProjectScope) -> str:
        """Tell the chat model which schedules exist and how to actually pause, resume or
        cancel one. Without this the model could only SAY it had paused a schedule
        (2026-10-02: 「已為您暫停」 while the 10-minute run kept firing)."""
        rows = self.approval_manager.trace_store.list_schedules(scope.name)
        if not rows:
            return ""
        tz = ZoneInfo(self.config.schedule_timezone)
        lines = []
        for row in rows:
            spec = self._row_spec(row)
            state = ""
            if row.get("paused_until"):
                state = "（已暫停" + self._paused_until_text(row["paused_until"], tz) + "）"
            lines.append(f"- {row['id']}：{describe_schedule(spec)}{state}。任務：{row['goal']}")
        return (
            "這個專案目前的排程：\n" + "\n".join(lines) + "\n"
            "你自己無法停止排程，只有輸出下列標記行（各自另起一行、放在回覆最後）harness 才會真的"
            "執行；沒有輸出標記就等於沒有做，所以絕對不要在沒輸出標記的情況下說「已暫停/已取消」。"
            f"暫停：「{self._SCHEDULE_PAUSE_MARKER}<排程id>::<恢復時間>」，恢復時間是台北時間的"
            "YYYY-MM-DD（當天 00:00 恢復）或 YYYY-MM-DD HH:MM，使用者沒講何時恢復就寫 indefinite"
            "（直到使用者要求恢復）；明天繼續就填明天的日期。"
            f"提前恢復：「{self._SCHEDULE_RESUME_MARKER}<排程id>」。"
            f"永久取消：「{self._SCHEDULE_CANCEL_MARKER}<排程id>」。"
            "使用者說「暫停」「明天再繼續」用暫停；說「取消」「不要了」用取消。\n\n"
        )

    @staticmethod
    def _row_spec(row: dict) -> ScheduleSpec:
        return spec_from_row(row)

    @classmethod
    def _paused_until_text(cls, paused_until: str, tz: ZoneInfo) -> str:
        until = datetime.fromisoformat(paused_until)
        if until >= cls._PAUSE_INDEFINITELY:
            return "，直到你要求恢復"
        return "，" + until.astimezone(tz).strftime("%Y-%m-%d %H:%M") + " 恢復"

    def _extract_schedule_controls(self, raw: str) -> tuple[str, list[tuple[str, str]]]:
        """Pull every pause/resume/cancel marker line out of a model reply."""
        markers = {
            "pause": self._SCHEDULE_PAUSE_MARKER,
            "resume": self._SCHEDULE_RESUME_MARKER,
            "cancel": self._SCHEDULE_CANCEL_MARKER,
        }
        kept, controls = [], []
        for line in raw.splitlines():
            stripped = line.strip()
            for action, marker in markers.items():
                if stripped.startswith(marker):
                    controls.append((action, stripped[len(marker):].strip()))
                    break
            else:
                kept.append(line)
        return "\n".join(kept).rstrip(), controls

    def _schedule_delivery(self, scope: ProjectScope, schedule_id: str, summary) -> ScheduleDelivery | None:
        """Have the Controller read a schedule run's result before it is shown (see
        trigger_review.py). None when no review is wired in or the schedule is gone — the
        result is then reported exactly as before. Never raises."""
        if self.review_caller_factory is None:
            return None
        try:
            trace_store = self.approval_manager.trace_store
            row = trace_store.get_schedule(schedule_id)
            if row is None:
                return None
            tz = ZoneInfo(self.config.schedule_timezone)
            now = datetime.now(timezone.utc)
            turns = trace_store.get_primary_session_turns(
                self.primary_session_mgr.primary_session_id_for_project(scope.name)
            )
            reports = [
                history_entry(t["content"])
                for t in turns
                if t["turn_type"] == SCHEDULE_REPORT and t["schedule_id"] == schedule_id
            ]
            history = list(reversed(reports))[:MAX_CONSECUTIVE_SILENT]
            last_user = next(
                (t for t in reversed(turns) if t["speaker"] == "user" and t["turn_type"] != SCHEDULE_TRIGGER),
                None,
            )
            last_operator = None
            if last_user is not None:
                minutes = int((now - datetime.fromisoformat(last_user["created_at"])).total_seconds() // 60)
                last_operator = f"{minutes // 60} 小時 {minutes % 60} 分鐘前" if minutes >= 60 else f"{minutes} 分鐘前"
            prompt = build_schedule_prompt(
                schedule_id=schedule_id,
                schedule_text=describe_schedule(self._row_spec(row)),
                goal=row["goal"],
                local_time=now.astimezone(tz).strftime("%Y-%m-%d %H:%M (%a)"),
                status=summary.status,
                result=summary.recommendation,
                history=history[:5],
                precedent=precedent_block(
                    trace_store, scope.name, preferences=self._operator_profile_context(scope)
                ),
                last_operator_message=last_operator,
            )
            return decide_schedule_delivery(
                caller=self.review_caller_factory(scope.name),
                status=summary.status,
                history=history,
                prompt=prompt,
            )
        except Exception:
            logger.exception(f"reviewing schedule {schedule_id}'s result failed; reporting it as before")
            return None

    def _pause_and_notify(
        self, scope: ProjectScope, schedule_id: str, summary, delivery: ScheduleDelivery, chat_id: int | None
    ) -> None:
        """The Controller judged that a schedule no longer has a reason to run: pause it
        (kept, not cancelled), say why, and give the operator a one-tap way to undo or finish
        it. The decision is recorded so a later review can learn which way they went."""
        trace_store = self.approval_manager.trace_store
        row = trace_store.get_schedule(schedule_id)
        if row is None or not row["active"]:
            return
        tz = ZoneInfo(self.config.schedule_timezone)
        now = datetime.now(timezone.utc)
        trace_store.pause_schedule(
            schedule_id,
            paused_until=self._PAUSE_INDEFINITELY,
            next_run_at=compute_next_run(self._row_spec(row), after=now, tz=tz),
        )
        trace_store.add_decision_record(
            project=scope.name,
            kind="schedule_pause",
            situation=delivery.reason,
            outcome=f"{CONTROLLER_OUTCOME_PREFIX}paused",
            subject=schedule_id,
        )
        text = (
            f"⏸ 排程 {schedule_id} 我先暫停了：{delivery.reason}\n"
            f"任務：{row['goal']}\n最近一輪結果：{summary.recommendation[:300]}\n\n"
            "要繼續就按「繼續排程」；不按的話它會維持暫停。"
        )
        keyboard = [
            [
                {"text": "▶️ 繼續排程", "callback_data": f"sched_resume:{schedule_id}"},
                {"text": "🗑 取消排程", "callback_data": f"sched_cancel:{schedule_id}"},
            ]
        ]
        targets = [chat_id] if chat_id is not None else sorted(self.allowed_chat_ids)
        for cid in targets:
            self.send_message_with_keyboard(cid, text, keyboard)
        self.primary_session_mgr.record_turn(
            project=scope.name, speaker="assistant", turn_type="chat", content=text
        )

    def _resolve_schedule_button(
        self, action: str, schedule_id: str, chat_id: int, message_id: int | None
    ) -> str:
        """A tap on the buttons of _pause_and_notify(). Resuming is the operator disagreeing
        with the Controller's judgment, cancelling is agreeing; either is kept as precedent."""
        trace_store = self.approval_manager.trace_store
        row = trace_store.get_schedule(schedule_id)
        scope = next((p for p in self.projects if row and p.name == row["project"]), None)
        if row is None or not row["active"] or scope is None:
            return f"找不到可操作的排程 {schedule_id}"
        paused = [
            r
            for r in trace_store.list_decision_records(scope.name, kind="schedule_pause", limit=20)
            if r["subject"] == schedule_id and r["outcome"].startswith(CONTROLLER_OUTCOME_PREFIX)
        ]
        situation = paused[0]["situation"] if paused else f"排程 {schedule_id} 被暫停"
        self._apply_schedule_control(scope, chat_id, "resume" if action == "resume" else "cancel", schedule_id)
        outcome = "resumed" if action == "resume" else "cancelled"
        record_id = trace_store.add_decision_record(
            project=scope.name,
            kind="schedule_pause",
            situation=situation,
            outcome=outcome,
            chosen_text="繼續排程" if action == "resume" else "取消排程",
            followed=action != "resume",
            subject=schedule_id,
        )
        self.primary_session_mgr.record_turn(
            project=scope.name,
            speaker="user",
            turn_type="decision",
            content=decision_turn_text(
                record_id=record_id,
                kind="schedule_pause",
                situation=situation,
                outcome="picked_other" if action == "resume" else "picked_recommended",
                chosen_text="繼續排程" if action == "resume" else "取消排程",
                recommended_label="暫停" if action == "resume" else None,
            ),
        )
        if message_id is not None:
            self.clear_message_keyboard(chat_id, message_id)
        return "已繼續排程" if action == "resume" else "已取消排程"

    def _apply_schedule_control(
        self, scope: ProjectScope, chat_id: int, action: str, payload: str
    ) -> None:
        """Execute one pause/resume/cancel directive and confirm the real outcome to the
        chat; failures are reported, never raised (this runs inside the reply path)."""
        trace_store = self.approval_manager.trace_store
        schedule_id, _, extra = (p.strip() for p in payload.partition("::"))
        row = trace_store.get_schedule(schedule_id)
        if row is None or not row["active"] or row["project"] != scope.name:
            self.send_message(chat_id, f"⚠️ 找不到可操作的排程 {schedule_id}，狀態沒有改變。")
            return
        tz = ZoneInfo(self.config.schedule_timezone)
        now = datetime.now(timezone.utc)
        spec = self._row_spec(row)
        try:
            if action == "cancel":
                trace_store.deactivate_schedule(schedule_id)
                self.send_message(chat_id, f"✅ 已取消排程 {schedule_id}。")
            elif action == "resume":
                next_run_at = compute_next_run(spec, after=now, tz=tz)
                trace_store.resume_schedule(schedule_id, next_run_at=next_run_at)
                local_next = next_run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                self.send_message(chat_id, f"▶️ 已恢復排程 {schedule_id}，下次執行：{local_next}。")
            else:
                if not extra or extra.lower() == "indefinite":
                    until = self._PAUSE_INDEFINITELY
                    next_run_at = compute_next_run(spec, after=now, tz=tz)
                else:
                    until = self._parse_pause_until(extra, tz)
                    next_run_at = compute_next_run(spec, after=max(until, now), tz=tz)
                    # compute_next_run is strictly after its anchor; an interval anchored
                    # on `until` would skip the first slot, so fire at `until` itself.
                    if spec.kind == "interval":
                        next_run_at = max(until, now)
                trace_store.pause_schedule(schedule_id, paused_until=until, next_run_at=next_run_at)
                self.send_message(
                    chat_id,
                    f"⏸ 已暫停排程 {schedule_id}"
                    f"{self._paused_until_text(until.isoformat(), tz)}。用 /schedules 查看。",
                )
        except Exception as exc:
            logger.exception(f"schedule {action} for {schedule_id} failed")
            self.send_message(chat_id, f"⚠️ 排程 {schedule_id} 操作失敗，狀態沒有改變：{exc}")

    @staticmethod
    def _parse_pause_until(value: str, tz: ZoneInfo) -> datetime:
        value = value.strip()
        fmt = "%Y-%m-%d %H:%M" if " " in value else "%Y-%m-%d"
        return datetime.strptime(value, fmt).replace(tzinfo=tz).astimezone(timezone.utc)

    def _create_schedule(self, scope: ProjectScope, chat_id: int, directive: str) -> None:
        """Parse a HARNESS_SCHEDULE_START directive's "<kind>::<spec>::<goal>" payload,
        persist it to TraceStore, and confirm back to the chat that raised it. Bad
        input (malformed kind/spec) is reported to the chat rather than raised —
        this runs inline in the chat-reply path, so it must never take down an
        otherwise-successful reply."""
        parts = directive.split("::", 2)
        if len(parts) != 3:
            self.send_message(
                chat_id,
                f"⚠️ 排程指令格式有誤，未建立：「{directive}」（預期格式：kind::spec::goal）",
            )
            return
        kind, spec, goal = (p.strip() for p in parts)
        if not goal:
            self.send_message(chat_id, "⚠️ 排程指令缺少任務內容，未建立。")
            return
        try:
            parsed = parse_schedule_spec(kind, spec)
            tz = ZoneInfo(self.config.schedule_timezone)
            next_run_at = compute_next_run(parsed, after=datetime.now(timezone.utc), tz=tz)
        except Exception as exc:
            self.send_message(chat_id, f"⚠️ 排程設定有誤，未建立：{exc}")
            return

        trace_store = self.approval_manager.trace_store
        schedule_id = trace_store.create_schedule(
            project=scope.name,
            workspace_id=scope.workspace_id,
            goal=goal,
            kind=parsed.kind,
            interval_seconds=parsed.interval_seconds,
            time_of_day=parsed.time_of_day,
            end_time_of_day=parsed.end_time_of_day,
            weekdays_only=parsed.weekdays_only,
            chat_id=chat_id,
            next_run_at=next_run_at,
        )
        local_next = next_run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        self.send_message(
            chat_id,
            f"⏰ 已建立排程 {schedule_id}（{describe_schedule(parsed)}）。"
            f"下次執行時間：{local_next}（{self.config.schedule_timezone}）。\n"
            f"任務內容：{goal}\n"
            f"用 /schedules 查看，/schedule_cancel {schedule_id} 取消。",
        )

    def run_due_schedule(self, schedule: dict) -> None:
        """Trigger one due schedule row from the gateway serve loop. Reuses the exact
        same governed-task-start path a chat-triggered "HARNESS_TASK_START::" goal
        does (_start_or_queue_task) — a scheduled run is not a different kind of task,
        just a different trigger — notifying back to the chat that created the
        schedule. Best-effort: a schedule row this gateway doesn't own any project for
        (project removed from the index after the schedule was created) is skipped
        and logged rather than raised, so one stale schedule can't break the tick."""
        scope = next((p for p in self.projects if p.name == schedule["project"]), None)
        if scope is None:
            logger.error(
                f"schedule {schedule['id']} targets unknown project "
                f"'{schedule['project']}'; skipping this run"
            )
            return
        chat_id = schedule.get("chat_id")
        if chat_id is None:
            logger.error(f"schedule {schedule['id']} has no chat_id to notify; skipping this run")
            return
        if self.approval_manager.trace_store.schedule_in_flight(schedule["id"]):
            # Its previous run hasn't finished: skip this occurrence rather than stack a
            # second copy of the same schedule. Logged for the chat model, not pushed.
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="system",
                turn_type=SCHEDULE_TRIGGER,
                content=f"排程 {schedule['id']} 觸發，但上一輪尚未結束，本輪略過。",
                schedule_id=schedule["id"],
            )
            logger.info(f"schedule {schedule['id']} is still running; skipping this occurrence")
            return
        self.primary_session_mgr.record_turn(
            project=scope.name,
            speaker="system",
            turn_type=SCHEDULE_TRIGGER,
            content=f"排程 {schedule['id']} 觸發：{schedule['goal']}",
            schedule_id=schedule["id"],
        )
        self._start_or_queue_task(scope, schedule["goal"], chat_id, schedule_id=schedule["id"])

    def list_schedules_message(self, scope: ProjectScope) -> str:
        """Deterministic /schedules reply — no model call, just a formatted read of
        TraceStore's schedules table for this project."""
        schedules = self.approval_manager.trace_store.list_schedules(scope.name)
        if not schedules:
            return f"「{scope.name}」目前沒有排程中的任務。"
        tz = ZoneInfo(self.config.schedule_timezone)
        lines = [f"「{scope.name}」目前的排程："]
        for row in schedules:
            spec = spec_from_row(row)
            next_local = datetime.fromisoformat(row["next_run_at"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
            state = f"，下次 {next_local}"
            if row.get("paused_until"):
                state = "，⏸ 已暫停" + self._paused_until_text(row["paused_until"], tz)
            lines.append(f"- {row['id']}：{describe_schedule(spec)}{state}。任務：{row['goal']}")
        lines.append("用 /schedule_cancel <id> 取消。")
        return "\n".join(lines)

    def cancel_schedule_message(self, schedule_id: str) -> str:
        if self.approval_manager.trace_store.deactivate_schedule(schedule_id):
            return f"✅ 已取消排程 {schedule_id}。"
        return f"⚠️ 找不到可取消的排程 {schedule_id}（可能已經被取消，或 ID 打錯了）。"

    def _run_new_task(self, scope: ProjectScope, conv_id: str, goal: str, schedule_id: str | None = None):
        task = TaskEnvelope(
            task_id=f"task_{conv_id}",
            workspace_id=scope.workspace_id,
            goal=goal,
            context_refs=[],
            context_items=self._project_context_items(scope),
            constraints=list(scope.off_limits or []),
            risk_level=scope.default_risk_level,
            source="telegram-chat",
        )
        profiles = load_role_profiles(self.config.role_profiles_file_for(scope.name))
        work_dir, worktree = self._provision_workdir(scope, conv_id)
        candidate_adapters = build_role_adapter_candidates(
            profiles=profiles,
            config=self.config,
            working_directory=work_dir,
            timeout_seconds=self.config.cli_timeout_seconds,
        )
        runner = AgentLoopRunner(
            adapters={p_id: items[0] for p_id, items in candidate_adapters.items()},
            fallback_adapters={p_id: items[1:] for p_id, items in candidate_adapters.items()},
            role_profiles=profiles,
            trace_store=self.approval_manager.trace_store,
            memtrace_client=self.memtrace_client,
            approval_manager=self.approval_manager,
            working_directory=work_dir,
            verify_command=scope.verify_command,
            verify_timeout_seconds=scope.verify_timeout_seconds or 1200,
            config=self.config,
            agent_loop_enabled=scope.agent_loop_enabled,
            alert_callback=self.notify_all_allowlisted,
            worktree=worktree,
            worktree_manager=self._worktree_manager() if worktree else None,
            repo_root=scope.working_directory if worktree else None,
            edit_lock=self._edit_lock_for(scope, conv_id, worktree, self.approval_manager.trace_store),
            create_approvals=schedule_id is None,
        )
        summary = runner.run(task, writeback=LOOP_DRAFT_WRITEBACK, conversation_id=conv_id)
        note = self._finalize_worktree(scope, conv_id, worktree, summary.status)
        if note and worktree and worktree.branch not in summary.recommendation:
            summary = replace(summary, recommendation=summary.recommendation + note)
        if schedule_id:
            delivery = self._schedule_delivery(scope, schedule_id, summary)
            if delivery is not None:
                self._schedule_deliveries[conv_id] = delivery
            content = (
                f"排程 {schedule_id} 執行完成（loop {summary.conversation_id}，"
                f"狀態 {summary.status}）：{summary.recommendation}"
            )
            if delivery is not None and delivery.action == "digest_only":
                content = silent_tag(delivery.reason) + content
            elif delivery is not None and delivery.action == "pause_and_notify":
                content += f"（Controller 判斷這個排程已無需執行，已暫停：{delivery.reason}）"
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="work_session_report",
                turn_type=SCHEDULE_REPORT,
                content=content,
                source_work_conversation_id=summary.conversation_id,
                schedule_id=schedule_id,
            )
        else:
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="work_session_report",
                turn_type="dev_report",
                content=f"Chat-triggered loop {summary.conversation_id} completed with status {summary.status}: {summary.recommendation}",
                source_work_conversation_id=summary.conversation_id,
            )
        return summary

    def _resume_approved_conversation(
        self, req_data: ApprovalRequestData, answer: str | None = None
    ) -> None:
        if req_data.reason == PROPOSAL_REASON:
            # A proposal from a work review resumes no task: the answer is the whole outcome. It
            # has been kept as precedent; implementing a change is a separate, explicit request.
            self.send_message(
                req_data.telegram_chat_id if req_data.telegram_chat_id is not None else sorted(self.allowed_chat_ids)[0],
                "📝 已記下你的選擇，之後的判斷會參考；harness 不會自己改動。想照這個做的話，直接告訴我，我會開成開發任務。",
            )
            return
        working_dir = Path(req_data.working_directory).resolve()
        if not working_dir.is_dir():
            logger.error(f"Cannot resume conversation {req_data.conversation_id}: working dir {working_dir} invalid")
            return
        matching_scope = self._scope_for_resume(req_data, working_dir)
        trace_store = self.approval_manager.trace_store
        outcome = trace_store.acquire_task_slot(
            req_data.workspace,
            req_data.conversation_id,
            max_slots=self._slot_limit(matching_scope) if matching_scope else 1,
        )
        if outcome == "already_running":
            # Either a double-tapped approval (a thread is running it) or a slot the
            # unattended scanner reserved while it waited for this very approval — that
            # one is handed over to the resumed run.
            if any(
                e["conversation_id"] == req_data.conversation_id
                for e in default_tracker.snapshot()
            ):
                self.notify_all_allowlisted(f"對話 {req_data.conversation_id} 已經在執行中。")
                return
            outcome = "acquired"
        if outcome != "acquired":
            trace_store.enqueue_task(
                workspace_id=req_data.workspace,
                kind="resume",
                payload={"request_id": req_data.id, "answer": answer},
                conversation_id=req_data.conversation_id,
                chat_id=req_data.telegram_chat_id,
            )
            self.notify_all_allowlisted(
                f"✅ 已核准，但「{matching_scope.name if matching_scope else req_data.workspace}」"
                "的任務位都在忙，已排入佇列，有空位時會自動繼續。"
            )
            return
        self._launch_resume(req_data, answer)

    def _scope_for_resume(self, req_data: ApprovalRequestData, working_dir: Path):
        return next(
            (s for s in self.projects if s.workspace_id == req_data.workspace or s.working_directory.resolve() == working_dir),
            None,
        )

    def _launch_resume(self, req_data: ApprovalRequestData, answer: str | None = None) -> None:
        """Continue an approved conversation in a background thread. The caller already
        holds the conversation's worker slot; this releases it when the run ends."""
        working_dir = Path(req_data.working_directory).resolve()
        matching_scope = self._scope_for_resume(req_data, working_dir)
        # Runs in a background thread — see _run() below — so this doesn't block the
        # poll loop for the minutes a governed run can take; say so up front rather
        # than leaving the user staring at silence after the "Approval update:
        # approved" message.
        self.notify_all_allowlisted(
            f"🔧 已核准，繼續執行「{matching_scope.name if matching_scope else req_data.workspace}」："
            f"{req_data.resume_goal or req_data.proposed_action or req_data.reason}"
        )
        # resume_goal (the original request) beats proposed_action (a human-readable
        # summary of why it stopped) — approving must continue the actual task, not
        # re-run with the stop reason as the new goal. Older rows predating this field
        # fall back to the old behavior.
        goal = req_data.resume_goal or req_data.proposed_action or req_data.reason
        if answer:
            goal = f"{goal}\n\nHuman clarification: {answer}"

        def _run() -> None:
            # A fresh TraceStore/connection per background thread — sqlite3
            # connections aren't safe to share across threads, and each call already
            # opens/closes its own connection, so this just avoids the main thread's
            # instance being touched concurrently.
            trace_store = TraceStore(self.config.trace_db_path)
            try:
                context_items = self._project_context_items(matching_scope) if matching_scope else []
                # An unattended-scan approval's stage_ref is the actual backlog item
                # id the operator just discussed and approved — hydrate its real
                # content (intent/constraints/acceptance_criteria, or the GitHub
                # issue body) into context, not just the bare id in the goal text,
                # so Dev knows what to build.
                if req_data.reason == "unattended_write" and req_data.stage_ref:
                    if req_data.stage_ref.startswith("gh:"):
                        issue_item = _hydrate_github_issue_context(
                            req_data.stage_ref, working_directory=working_dir
                        )
                        if issue_item:
                            context_items = context_items + [issue_item]
                    elif self.memtrace_client:
                        try:
                            context_items = context_items + self.memtrace_client.hydrate_context_refs(
                                workspace_id=req_data.workspace,
                                refs=[req_data.stage_ref],
                                run_id=req_data.conversation_id,
                                stage="scan_task_resume",
                            )
                        except Exception:
                            logger.exception(
                                f"failed to hydrate Task Node context for {req_data.stage_ref}"
                            )
                task = TaskEnvelope(
                    task_id=f"task_{req_data.conversation_id}",
                    workspace_id=req_data.workspace,
                    goal=goal,
                    context_refs=[req_data.stage_ref] if req_data.stage_ref else [],
                    context_items=context_items,
                    constraints=list(matching_scope.off_limits or []) if matching_scope else [],
                    risk_level="medium",
                    source="telegram-approval-resume",
                )
                profiles_file = (
                    self.config.role_profiles_file_for(matching_scope.name) if matching_scope else None
                )
                profiles = load_role_profiles(profiles_file)
                worktree: TaskWorktree | None = None
                run_dir = working_dir
                if matching_scope is not None:
                    # Same worktree as the run that stopped (the approval carries its
                    # path), or a fresh one for a conversation that never had one.
                    run_dir, worktree = self._provision_workdir(
                        matching_scope, req_data.conversation_id
                    )
                candidate_adapters = build_role_adapter_candidates(
                    profiles=profiles,
                    config=self.config,
                    working_directory=run_dir,
                    timeout_seconds=self.config.cli_timeout_seconds,
                )
                runner = AgentLoopRunner(
                    adapters={p_id: items[0] for p_id, items in candidate_adapters.items()},
                    fallback_adapters={p_id: items[1:] for p_id, items in candidate_adapters.items()},
                    role_profiles=profiles,
                    trace_store=trace_store,
                    memtrace_client=self.memtrace_client,
                    approval_manager=self.approval_manager,
                    working_directory=run_dir,
                    verify_command=matching_scope.verify_command if matching_scope else None,
                    verify_timeout_seconds=(
                        (matching_scope.verify_timeout_seconds if matching_scope else None) or 1200
                    ),
                    config=self.config,
                    agent_loop_enabled=(
                        matching_scope.agent_loop_enabled if matching_scope else True
                    ),
                    alert_callback=self.notify_all_allowlisted,
                    worktree=worktree,
                    worktree_manager=self._worktree_manager() if worktree else None,
                    repo_root=matching_scope.working_directory if worktree and matching_scope else None,
                    edit_lock=(
                        self._edit_lock_for(matching_scope, req_data.conversation_id, worktree, trace_store)
                        if matching_scope is not None
                        else None
                    ),
                )
                summary = runner.run(
                    task,
                    writeback=LOOP_DRAFT_WRITEBACK,
                    conversation_id=req_data.conversation_id,
                )
                if matching_scope is not None:
                    note = self._finalize_worktree(
                        matching_scope, req_data.conversation_id, worktree, summary.status
                    )
                    if note and worktree and worktree.branch not in summary.recommendation:
                        summary = replace(summary, recommendation=summary.recommendation + note)
                project_name = matching_scope.name if matching_scope else req_data.workspace

                self.primary_session_mgr.record_turn(
                    project=project_name,
                    speaker="work_session_report",
                    turn_type="dev_report",
                    content=f"Resumed loop {summary.conversation_id} completed with status {summary.status}: {summary.recommendation}",
                    source_work_conversation_id=summary.conversation_id,
                )
                self._report_run_outcome(
                    conv_id=req_data.conversation_id,
                    summary=summary,
                    final_text=(
                        f"🔄 對話 {req_data.conversation_id} 已繼續執行。結果：{summary.status}"
                        f"（{summary.recommendation}）\n\n🧩 {self._model_summary(summary)}"
                    ),
                )
            except Exception:
                logger.exception(f"background resume for conversation {req_data.conversation_id} failed")
                self.notify_all_allowlisted(
                    f"⚠️ 對話 {req_data.conversation_id} 恢復執行時發生未預期錯誤，請查看日誌。"
                )
            finally:
                trace_store.release_workspace_lock(req_data.workspace, req_data.conversation_id)
                default_tracker.unregister(thread)
                if matching_scope is not None:
                    self._drain_task_queue(matching_scope)

        thread = threading.Thread(
            target=_run, name=f"agent-loop-resume-{req_data.conversation_id}", daemon=True
        )
        thread.start()
        default_tracker.register(
            thread,
            workspace_id=req_data.workspace,
            conversation_id=req_data.conversation_id,
            trace_store=self.approval_manager.trace_store,
        )

    def poll_once(self, timeout: int = 1) -> int:
        updates = self.get_updates(timeout=timeout)
        processed = 0
        for update in updates:
            # Advance and persist the offset even for updates process_update() drops
            # (e.g. non-allowlisted chats) so a dropped message doesn't get refetched
            # forever; process_update() bumps self.offset before its allowlist check.
            res = self.process_update(update)
            if res is not None:
                processed += 1
        if self._offset_key:
            self.approval_manager.trace_store.set_telegram_offset(self._offset_key, self.offset)
        return processed
