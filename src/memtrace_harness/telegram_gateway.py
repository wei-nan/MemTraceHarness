from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pathlib import Path

from memtrace_harness.adapter_factory import build_role_adapter_candidates
from memtrace_harness.approval import ApprovalRequestData
from memtrace_harness.inflight import default_tracker
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.primary_session import OPERATOR_PROFILE_TITLE
from memtrace_harness.role_profiles import load_role_profiles
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
    def _approval_keyboard(request_id: str) -> list[list[dict[str, str]]]:
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
                cid, req.format_telegram_message(), self._approval_keyboard(req.id)
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

        # Plain free-text (not a "/" command, not a "!" task trigger) gets one chance
        # to resolve as an answer to a pending approval before falling through to
        # ordinary chat — either because it's a native swipe-reply to the approval
        # message (unambiguous), or because there's exactly one pending approval for
        # this bot's projects (still unambiguous — nothing to disambiguate). This is
        # deliberately scoped to "clarify" only, never "approve"/"reject": clarify just
        # feeds the answer back into the loop and can't by itself authorize a write,
        # so treating free text as clarify by default carries none of the risk that
        # inferring "approve" from casual wording would.
        if not text.startswith("/") and not text.startswith("!"):
            clarify_target = self._find_clarify_target(message, chat_id)
            if clarify_target:
                return self._resolve_approval_action(clarify_target.id, "clarify", chat_id, text)

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
            info = f"Harness Gateway 已上線。\n已註冊專案數：{len(self.projects)}"
            self.send_message(chat_id, info)
            return info

        if result.kind == "chat":
            scope = result.project_scope
            assert scope is not None
            self.primary_session_mgr.record_turn(
                project=scope.name, speaker="user", turn_type="chat", content=text
            )
            # A quick reply still takes a few seconds (a real CLI call, not instant) —
            # show Telegram's native "typing..." indicator so the wait isn't silent.
            self.send_chat_action(chat_id, "typing")
            reply = self._quick_chat_reply(scope, text)
            self.primary_session_mgr.record_turn(
                project=scope.name, speaker="assistant", turn_type="chat", content=reply
            )
            self.send_message(chat_id, reply)
            return reply

        if result.kind == "task":
            scope = result.project_scope
            assert scope is not None
            # turn_type="decision" (not "chat"): the user explicitly asked to trigger a
            # governed task ("!" prefix) — that's exactly the kind of turn cold-memory
            # consolidation should promote to MemTrace, not filter out as small talk.
            self.primary_session_mgr.record_turn(
                project=scope.name,
                speaker="user",
                turn_type="decision",
                content=text,
            )
            goal = result.task_goal or text
            return self._start_or_queue_task(scope, goal, chat_id)

    def _resolve_approval_action(
        self, request_id: str, action: str, chat_id: int, reason_or_answer: str | None
    ) -> str:
        """Shared by the /approve|/reject|/clarify text commands, the inline-keyboard
        button callbacks, and the swipe-reply/single-pending clarify fallback — one
        place that resolves an approval and reacts to the outcome, so those three
        entry points can't drift out of sync with each other."""
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

    def _find_clarify_target(
        self, message: dict[str, Any], chat_id: int
    ) -> ApprovalRequestData | None:
        """Resolve free text to the pending approval it's answering, without the human
        ever typing an ID. Two ways, both unambiguous by construction (never a guess):
        1. A native swipe-reply to the approval's own message — Telegram's
           reply_to_message tells us exactly which one.
        2. Exactly one pending approval exists across this bot's projects — there's
           nothing to disambiguate, so any plain message can only be about that one.
        Returns None (falls through to ordinary chat/task routing) whenever more than
        one approval is pending and there's no reply match — guessing among several
        would risk answering the wrong one."""
        reply_to = message.get("reply_to_message")
        if reply_to and reply_to.get("message_id") is not None:
            req = self.approval_manager.get_by_telegram_message(chat_id, reply_to["message_id"])
            if req and req.status == "pending":
                return req
        pending: list[ApprovalRequestData] = []
        for scope in self.projects:
            pending.extend(
                ApprovalRequestData(**data)
                for data in self.approval_manager.trace_store.list_pending_approvals_for_workspace(
                    scope.workspace_id
                )
            )
        return pending[0] if len(pending) == 1 else None

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

        if self.config.unattended_write_requires_approval:
            req = self.approval_manager.request_approval(
                conversation_id=conv_id,
                workspace=ws_id,
                working_directory=str(scope.working_directory),
                reason="unattended_write",
                proposed_action=goal,
                resume_goal=goal,
            )
            self.notify_approval_request(req)
            # Lock stays held until the approve/reject button (or /approve, /reject)
            # resolves the request.
            return req.format_telegram_message()

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
                self.send_message(chat_id, msg)
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
            )
            node = next(
                (n for n in results if isinstance(n, dict) and n.get("title") == OPERATOR_PROFILE_TITLE and n.get("id")),
                None,
            )
            if not node:
                return ""
            full = self.memtrace_client.get_node(
                workspace_id=self.config.operator_preference_workspace_id, node_id=str(node["id"])
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
        see _identity_context(), still used as-is for _quick_chat_reply(). Off-limits
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

    def _quick_chat_reply(self, scope: ProjectScope, text: str) -> str:
        """A read-only, no-approval, single quick CLI call for plain conversation —
        deliberately NOT the governed Controller/Planner/RedTeam/Developer loop. Tries
        that project's own chat_candidates_for() list in order (its own
        HARNESS_CHAT_PROVIDER_<PROJECT>/_MODEL/_FALLBACKS if set, else the shared
        HARNESS_CHAT_PROVIDER/MODEL/FALLBACKS) — chat is cheap and low-stakes, so each
        project can just pick whatever model is good enough and cheapest, no vendor
        lock, and a chat-only outage on one provider falls through to the next."""
        from memtrace_harness.cli_process import CliProcessRunner

        candidates = self.config.chat_candidates_for(scope.name)
        if not candidates:
            return "（快速對話尚未設定：HARNESS_CHAT_PROVIDER 沒有值)"
        prompt = (
            f"{self._identity_context(scope)}\n\n---\n\n"
            "使用者現在是在聊天，不是在指派工作——這只是一句快速、唯讀的回覆，不是任務。"
            "不要提出修改建議、不要宣稱你執行了什麼。一律使用繁體中文回覆，簡短即可。"
            "如果使用者其實是想要你動手做事，告訴他們在訊息前面加上「!」來觸發真正受控的任務。\n\n"
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
                        f"quick chat reply fell back to {provider}/{model} for project "
                        f"'{scope.name}' after: {'; '.join(failures)}"
                    )
                return f"{result.stdout.strip()}\n\n🧩 {provider}/{model or '預設模型'}"
            detail = result.error or result.stderr.strip() or f"exit code {result.return_code}"
            failures.append(f"{provider}/{model or '預設模型'}：{detail}")

        joined = "；".join(failures)
        return f"（快速回覆失敗：{joined}。改用「!」開頭可觸發完整任務流程。)"

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
                task = TaskEnvelope(
                    task_id=f"task_{req_data.conversation_id}",
                    workspace_id=req_data.workspace,
                    goal=goal,
                    context_refs=[],
                    context_items=(
                        self._project_context_items(matching_scope) if matching_scope else []
                    ),
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
                self.notify_all_allowlisted(
                    f"🔄 對話 {req_data.conversation_id} 已繼續執行。結果：{summary.status}"
                    f"（{summary.recommendation[:120]}）\n\n🧩 {self._model_summary(summary)}"
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
