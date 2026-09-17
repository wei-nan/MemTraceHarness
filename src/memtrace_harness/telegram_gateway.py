from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pathlib import Path

from memtrace_harness.adapter_factory import build_role_adapter_candidates
from memtrace_harness.approval import INFO_NEEDED_REASONS, ApprovalRequestData
from memtrace_harness.inflight import default_tracker
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.primary_session import OPERATOR_PROFILE_TITLE
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schedule import ScheduleSpec, compute_next_run, describe_schedule, parse_schedule_spec
from memtrace_harness.schemas import ContextItem, TaskEnvelope
from memtrace_harness.trace_store import TraceStore

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalManager
    from memtrace_harness.chat_triage import ChatTriage
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.primary_session import PrimarySessionManager
    from memtrace_harness.scope import ProjectScope

logger = logging.getLogger(__name__)


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
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
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
    def _approval_keyboard(request_id: str, reason: str) -> list[list[dict[str, str]]]:
        if reason in INFO_NEEDED_REASONS:
            # No "approve" button here — see INFO_NEEDED_REASONS: a bare approve
            # would just resume with the same unchanged goal that produced this
            # question in the first place. Answering is a text reply, not a tap.
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
                cid, req.format_telegram_message(), self._approval_keyboard(req.id, req.reason)
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
            self.primary_session_mgr.record_turn(
                project=scope.name, speaker="user", turn_type="chat", content=text
            )
            self.send_chat_action(chat_id, "typing")

            # A native swipe-reply to a pending approval's own message is
            # unambiguous (the human deliberately picked that message to reply
            # to) — resolve it as a clarify without involving the model at all.
            # This is the only mechanical, non-model path left for touching a
            # pending approval from plain text; anything else needs the
            # /approve, /reject, /clarify commands or the inline buttons.
            reply_to = message.get("reply_to_message")
            if reply_to and reply_to.get("message_id") is not None:
                pending = self.approval_manager.get_by_telegram_message(chat_id, reply_to["message_id"])
                if pending and pending.status == "pending":
                    return self._resolve_approval_action(pending.id, "clarify", chat_id, text)

            return self._chat_reply_and_maybe_start_task(scope, chat_id, text)

    def _resolve_approval_action(
        self, request_id: str, action: str, chat_id: int, reason_or_answer: str | None
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
            if req_data.telegram_chat_id is not None and req_data.telegram_message_id is not None:
                self.clear_message_keyboard(req_data.telegram_chat_id, req_data.telegram_message_id)
            if req_data.status == "approved":
                self._resume_approved_conversation(req_data, reason_or_answer)
            elif req_data.status in {"rejected", "expired"}:
                self.approval_manager.trace_store.release_workspace_lock(req_data.workspace)
        return msg

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
        action, _, request_id = data.partition(":")
        if not action or not request_id:
            if callback_id:
                self.answer_callback_query(callback_id, text="無法辨識的按鈕")
            return None
        result = self._resolve_approval_action(request_id, action, chat_id, None)
        if callback_id:
            self.answer_callback_query(callback_id, text=result[:200])
        return result

    def _start_or_queue_task(self, scope: ProjectScope, goal: str, chat_id: int) -> str:
        ws_id = scope.workspace_id
        trace_store = self.approval_manager.trace_store
        if trace_store.get_workspace_lock(ws_id) is not None:
            msg = f"工作區 {ws_id}（{scope.name}）目前有任務正在執行中，請等它結束後再試一次。"
            self.send_message(chat_id, msg)
            return msg

        conv_id = f"chat_{uuid4().hex[:8]}"
        if not trace_store.acquire_workspace_lock(ws_id, conv_id):
            msg = f"工作區 {ws_id}（{scope.name}）目前有任務正在執行中，請等它結束後再試一次。"
            self.send_message(chat_id, msg)
            return msg

        # No separate approval step before starting: the decision to do real
        # development work is made in conversation with the model itself (see
        # _chat_reply_and_maybe_start_task()) — by the time this is called, that
        # decision has already been made together with the human in the chat.
        # Runs in a background thread so this bot's poll loop — and therefore ordinary
        # chat on it — isn't blocked for the minutes a governed loop can take. The
        # workspace lock (already acquired above) is what actually prevents a second
        # task from starting on the same workspace while this one runs; it's released
        # inside the thread once the run finishes, not here.
        self.send_message(
            chat_id,
            f"🔧 已開始執行「{scope.name}」的任務，可能需要幾分鐘，完成後會通知你（這段期間仍可以正常對話）。",
        )

        def _run() -> None:
            try:
                summary = self._run_new_task(scope, conv_id, goal)
                msg = (
                    f"✅「{scope.name}」的任務已完成。狀態：{summary.status}。{summary.recommendation[:200]}\n\n"
                    f"🧩 {self._model_summary(summary)}"
                )
                self._report_run_outcome(
                    conv_id=conv_id, summary=summary, final_text=msg, chat_id=chat_id
                )
            except Exception:
                logger.exception(f"background agent loop for '{scope.name}' ({conv_id}) failed")
                self.send_message(chat_id, f"⚠️「{scope.name}」的任務執行時發生未預期錯誤，請查看日誌。")
            finally:
                trace_store.release_workspace_lock(ws_id)
                default_tracker.unregister(thread)

        thread = threading.Thread(target=_run, name=f"agent-loop-{conv_id}", daemon=True)
        thread.start()
        default_tracker.register(
            thread, workspace_id=ws_id, conversation_id=conv_id, trace_store=trace_store
        )
        return f"任務已在背景開始執行：{goal[:80]}"

    def _identity_context(self, scope: ProjectScope) -> str:
        memory_ws = self.config.memory_workspace_id_for(scope.name, scope.workspace_id)
        parts = [
            f"You are the Harness agent for project '{scope.name}' (workspace "
            f"`{scope.workspace_id}`). This is the project's harness-scope.md, its source of "
            f"identity, default risk level, and off-limits rules — do not ask the user to repeat "
            f"it:\n\n{scope.raw_markdown.strip()}",
            f"Cold-memory workspace: `{memory_ws}` (\"Harness Memory\" in MemTrace) — where "
            "consolidated conversation history and decisions get written as draft evidence "
            "by a periodic background pass. This is separate from the project's own spec "
            "workspace above. If you are aware of a local Claude Code project-memory "
            "directory (e.g. under ~/.claude/projects/.../memory/) from your own tool "
            "context, that is unrelated to the Harness and must NOT be listed as one of "
            "this project's registered knowledge bases — only the workspaces named here are.",
            "Language policy: every reply, recommendation, and summary sent back to the "
            "human — in every stage of this run — must be written in Traditional Chinese "
            "(繁體中文，台灣用語與正體字), never Simplified Chinese and never simplified "
            "phrasing/vocabulary, regardless of what language the human's own message used.",
        ]
        rehydration = self.primary_session_mgr.get_rehydration_context(scope.name)
        if rehydration:
            parts.append(f"Prior discussion for this project (open items may still need action):\n\n{rehydration}")
        operator_profile = self._operator_profile_context()
        if operator_profile:
            parts.append(
                "Operator preference profile — how this human wants you to work with "
                "them, learned from prior conversations across every project, not just "
                f"this one. Follow it same as any explicit instruction:\n\n{operator_profile}"
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

    def _operator_profile_context(self) -> str:
        """Cross-project: the operator's own preference profile, not scoped to any one
        project's workspace. Best-effort — MemTrace being unreachable must never break
        an ordinary chat reply or task, so failures here are swallowed."""
        if not self.memtrace_client or not self.config.operator_preference_workspace_id:
            return ""
        try:
            results = self.memtrace_client.search_nodes(
                workspace_id=self.config.operator_preference_workspace_id,
                query=OPERATOR_PROFILE_TITLE,
                stage="chat_context",
            )
            node = next(
                (n for n in results if isinstance(n, dict) and n.get("title") == OPERATOR_PROFILE_TITLE and n.get("id")),
                None,
            )
            if not node:
                return ""
            full = self.memtrace_client.get_node(
                workspace_id=self.config.operator_preference_workspace_id, node_id=str(node["id"]),
                stage="chat_context",
            )
            return str(full.get("body") or "").strip()
        except Exception:
            logger.exception("Failed to fetch operator preference profile; continuing without it")
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
            )
        ]
        rehydration = self.primary_session_mgr.get_rehydration_context(scope.name)
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
        if chat_id is not None:
            self.send_message(chat_id, final_text)
        else:
            self.notify_all_allowlisted(final_text)

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
    _SCHEDULE_START_MARKER = "HARNESS_SCHEDULE_START::"

    def _chat_reply_and_maybe_start_task(self, scope: ProjectScope, chat_id: int, text: str) -> str:
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
            "如果，而且只有在，根據這則訊息（以及前面的對話），你確信使用者現在真的是要你"
            "動手進行開發——修改這個 repo 的程式碼、實際落地某個具體改動——才在回覆的最後"
            f"另起一行，格式為「{self._TASK_START_MARKER}<一句話描述要做的具體任務>」，交給"
            "受控的 Controller/Planner/RedTeam/Developer 流程去執行。單純聊天、討論、還在"
            "釐清需求、或只是在問問題時，絕對不要輸出這一行——不確定就不要輸出。\n\n"
            "如果，而且只有在，使用者明確要你之後每隔一段時間、或每天/每個工作日固定時間"
            "重複執行某件事——才在回覆的最後另起一行，格式為「"
            f"{self._SCHEDULE_START_MARKER}<kind>::<spec>::<一句話描述要做的具體任務>」，"
            "其中 kind 是 interval、daily 或 weekdays 三選一：interval 的 spec 是週期秒數"
            "（例如 3600 代表每小時一次，最少 60 秒）；daily 的 spec 是每天固定時間，"
            "24 小時制 HH:MM（例如 09:00）；weekdays 的 spec 跟 daily 一樣但只在週一到"
            "週五觸發。同一則回覆不要同時輸出這一行和上面的任務啟動標記。不確定使用者是否"
            "真的要排程、或排程細節（週期、時間）還沒問清楚時，絕對不要輸出這一行，先在對話"
            "裡把細節問清楚。\n\n"
            f"使用者訊息：{text}"
        )
        failures: list[str] = []
        for provider, model in candidates:
            cmd = [self.config.command_for(provider)]
            if model:
                cmd.extend(["--model", model])
            cmd.extend(["--print", prompt])
            result = CliProcessRunner().run(cmd, cwd=scope.working_directory, timeout_seconds=60)
            if result.return_code == 0 and result.stdout.strip():
                if failures:
                    logger.warning(
                        f"chat reply fell back to {provider}/{model} for project "
                        f"'{scope.name}' after: {'; '.join(failures)}"
                    )
                reply_text, goal = self._extract_task_start(result.stdout.strip())
                reply_text, schedule_directive = self._extract_schedule_start(reply_text)
                attribution = f"🧩 {provider}/{model or '預設模型'}"
                reply = f"{reply_text}\n\n{attribution}" if reply_text else attribution
                self.primary_session_mgr.record_turn(
                    project=scope.name, speaker="assistant", turn_type="chat", content=reply_text or reply
                )
                self.send_message(chat_id, reply)
                if goal:
                    self.primary_session_mgr.record_turn(
                        project=scope.name, speaker="user", turn_type="decision", content=goal
                    )
                    self._start_or_queue_task(scope, goal, chat_id)
                if schedule_directive:
                    self._create_schedule(scope, chat_id, schedule_directive)
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

    @classmethod
    def _extract_task_start(cls, raw: str) -> tuple[str, str | None]:
        """Pull a trailing "HARNESS_TASK_START::<goal>" line out of a model reply."""
        return cls._extract_marker_line(raw, cls._TASK_START_MARKER)

    @classmethod
    def _extract_schedule_start(cls, raw: str) -> tuple[str, str | None]:
        """Pull a trailing "HARNESS_SCHEDULE_START::<kind>::<spec>::<goal>" line out of
        a model reply."""
        return cls._extract_marker_line(raw, cls._SCHEDULE_START_MARKER)

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
            chat_id=chat_id,
            next_run_at=next_run_at,
        )
        local_next = next_run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        self.send_message(
            chat_id,
            f"⏰ 已建立排程 {schedule_id}（{describe_schedule(parsed)}）。"
            f"下次執行時間：{local_next}（{self.config.schedule_timezone}）。\n"
            f"任務內容：{goal[:120]}\n"
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
        self.primary_session_mgr.record_turn(
            project=scope.name,
            speaker="system",
            turn_type="decision",
            content=f"排程 {schedule['id']} 觸發：{schedule['goal']}",
        )
        self._start_or_queue_task(scope, schedule["goal"], chat_id)

    def list_schedules_message(self, scope: ProjectScope) -> str:
        """Deterministic /schedules reply — no model call, just a formatted read of
        TraceStore's schedules table for this project."""
        schedules = self.approval_manager.trace_store.list_schedules(scope.name)
        if not schedules:
            return f"「{scope.name}」目前沒有排程中的任務。"
        tz = ZoneInfo(self.config.schedule_timezone)
        lines = [f"「{scope.name}」目前的排程："]
        for row in schedules:
            spec = ScheduleSpec(
                kind=row["kind"],
                interval_seconds=row["interval_seconds"],
                time_of_day=row["time_of_day"],
            )
            next_local = datetime.fromisoformat(row["next_run_at"]).astimezone(tz).strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"- {row['id']}：{describe_schedule(spec)}，下次 {next_local}。任務：{row['goal'][:80]}"
            )
        lines.append("用 /schedule_cancel <id> 取消。")
        return "\n".join(lines)

    def cancel_schedule_message(self, schedule_id: str) -> str:
        if self.approval_manager.trace_store.deactivate_schedule(schedule_id):
            return f"✅ 已取消排程 {schedule_id}。"
        return f"⚠️ 找不到可取消的排程 {schedule_id}（可能已經被取消，或 ID 打錯了）。"

    def _run_new_task(self, scope: ProjectScope, conv_id: str, goal: str):
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
            trace_store=self.approval_manager.trace_store,
            memtrace_client=self.memtrace_client,
            approval_manager=self.approval_manager,
            working_directory=scope.working_directory,
            verify_command=scope.verify_command,
            verify_timeout_seconds=scope.verify_timeout_seconds or 1200,
            config=self.config,
            agent_loop_enabled=scope.agent_loop_enabled,
        )
        summary = runner.run(task, writeback=True, conversation_id=conv_id)
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
        working_dir = Path(req_data.working_directory).resolve()
        if not working_dir.is_dir():
            logger.error(f"Cannot resume conversation {req_data.conversation_id}: working dir {working_dir} invalid")
            return
        matching_scope = next(
            (s for s in self.projects if s.workspace_id == req_data.workspace or s.working_directory.resolve() == working_dir),
            None,
        )
        # Runs in a background thread — see _run() below — so this doesn't block the
        # poll loop for the minutes a governed run can take; say so up front rather
        # than leaving the user staring at silence after the "Approval update:
        # approved" message.
        self.notify_all_allowlisted(
            f"🔧 已核准，開始執行「{matching_scope.name if matching_scope else req_data.workspace}」，"
            "可能需要幾分鐘，完成後會通知你（這段期間仍可以正常對話）。"
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
                    memtrace_client=self.memtrace_client,
                    approval_manager=self.approval_manager,
                    working_directory=working_dir,
                    verify_command=matching_scope.verify_command if matching_scope else None,
                    verify_timeout_seconds=(
                        (matching_scope.verify_timeout_seconds if matching_scope else None) or 1200
                    ),
                    config=self.config,
                    agent_loop_enabled=(
                        matching_scope.agent_loop_enabled if matching_scope else True
                    ),
                )
                summary = runner.run(
                    task,
                    writeback=True,
                    conversation_id=req_data.conversation_id,
                )
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
                        f"（{summary.recommendation[:120]}）\n\n🧩 {self._model_summary(summary)}"
                    ),
                )
            except Exception:
                logger.exception(f"background resume for conversation {req_data.conversation_id} failed")
                self.notify_all_allowlisted(
                    f"⚠️ 對話 {req_data.conversation_id} 恢復執行時發生未預期錯誤，請查看日誌。"
                )
            finally:
                trace_store.release_workspace_lock(req_data.workspace)
                default_tracker.unregister(thread)

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
