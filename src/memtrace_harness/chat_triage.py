from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.scope import ProjectScope


@dataclass
class TriageResult:
    kind: str  # "approval_response", "chat", "status", "out_of_scope", "unrecognized",
    # "schedule_list", "schedule_cancel"
    project_scope: ProjectScope | None
    approval_id: str | None = None
    approval_action: str | None = None  # "approve", "reject", "clarify"
    approval_reason: str | None = None
    task_goal: str | None = None
    rejection_message: str | None = None
    schedule_id: str | None = None


class ChatTriage:
    def __init__(
        self, projects: list[ProjectScope], config: HarnessConfig | None = None
    ) -> None:
        self.projects = projects
        self.config = config

    def triage_message(self, text: str, default_project: str | None = None) -> TriageResult:
        cleaned = text.strip()

        # Check for commands like /approve, /reject, /clarify
        if cleaned.startswith("/approve") or cleaned.startswith("/reject") or cleaned.startswith("/clarify"):
            parts = cleaned.split(maxsplit=2)
            cmd = parts[0][1:].lower()
            if len(parts) < 2:
                return TriageResult(
                    kind="out_of_scope",
                    project_scope=None,
                    rejection_message=f"指令 {parts[0]} 需要核准請求 ID（例如 {parts[0]} appr_123456）",
                )
            appr_id = parts[1]
            reason = parts[2] if len(parts) > 2 else None
            return TriageResult(
                kind="approval_response",
                project_scope=self._find_project(default_project),
                approval_id=appr_id,
                approval_action=cmd,
                approval_reason=reason,
            )

        # /schedules and /schedule_cancel are deterministic, structured operations
        # (list/cancel a row) — unlike starting a schedule, which needs the model to
        # judge conversational intent, these never need a model call.
        if cleaned.startswith("/schedule_cancel"):
            parts = cleaned.split(maxsplit=1)
            if len(parts) < 2:
                return TriageResult(
                    kind="out_of_scope",
                    project_scope=None,
                    rejection_message="指令 /schedule_cancel 需要排程 ID（例如 /schedule_cancel sched_abc123）",
                )
            return TriageResult(
                kind="schedule_cancel", project_scope=None, schedule_id=parts[1].strip()
            )

        if cleaned.startswith("/schedules"):
            matched = self._match_project_from_text(cleaned) or self._find_project(default_project)
            if not matched and len(self.projects) == 1:
                matched = self.projects[0]
            return TriageResult(kind="schedule_list", project_scope=matched)

        # Check for scope/boundary violations
        lower_text = cleaned.lower()
        if any(keyword in lower_text for keyword in ["open a pr", "pull request", "check my email", "send email"]):
            return TriageResult(
                kind="out_of_scope",
                project_scope=None,
                rejection_message="這個請求超出 Harness 遠端操作的範圍（例如開 GitHub PR、git push 以外的外部服務都不支援）。",
            )

        # Match project
        matched_project = self._match_project_from_text(cleaned) or self._find_project(default_project)
        if not matched_project and len(self.projects) == 1:
            matched_project = self.projects[0]

        if not matched_project:
            return TriageResult(
                kind="unrecognized",
                project_scope=None,
                rejection_message="無法對應到任何已註冊的專案（找不到符合的 harness-scope.md）。",
            )

        # off_limits is NOT enforced here as a mechanical keyword block any more
        # (2026-09-17, explicit user request): a substring match can't tell "the key
        # is in .env, use it" (safe reference by name) apart from an actual leaked
        # value, so it only ever blocked the safe case — the unsafe case (a raw
        # secret string) usually doesn't even contain the keyword. Enforcement moves
        # to the model, same trust model as HARNESS_TASK_START: harness-scope.md's
        # full text (including off_limits) is already part of the chat prompt's
        # identity context (see TelegramGateway._identity_context()), and the prompt
        # explicitly tells the model those are boundaries to respect while replying,
        # not just background text. off_limits still travels as TaskEnvelope.constraints
        # to the governed Agent Loop roles for real work — that path is unaffected.

        if lower_text in {"status", "help", "/status", "/help"}:
            return TriageResult(kind="status", project_scope=matched_project)

        # Every other plain message is just chat: it goes straight to the model,
        # with no prefix/slash-command marker and no separate classification step.
        # There is no more "!"/"/task" trigger that mechanically queues a governed
        # write task — whether to actually start development work is now a
        # judgment call the model makes inside the conversation itself (see
        # TelegramGateway._chat_reply_and_maybe_start_task()), same conversation
        # the human is already having with it, not a separate command.
        return TriageResult(
            kind="chat",
            project_scope=matched_project,
            task_goal=cleaned,
        )

    def _match_project_from_text(self, text: str) -> ProjectScope | None:
        text_lower = text.lower()
        for p in self.projects:
            if p.name.lower() in text_lower or p.workspace_id.lower() in text_lower:
                return p
        if self.config and len(self.projects) > 1:
            return self.classify_project_with_llm(text)
        return None

    def classify_project_with_llm(self, text: str) -> ProjectScope | None:
        if not self.config or not self.config.chat_provider or not self.projects:
            return None
        from memtrace_harness.cli_process import CliProcessRunner
        runner = CliProcessRunner()
        proj_names = [p.name for p in self.projects]
        prompt = (
            f"Classify which project scope the following request targets: '{text}'. "
            f"Available options: {', '.join(proj_names)}. "
            "Reply with EXACTLY one project name from the list, or 'NONE' if no project matches."
        )
        provider = self.config.chat_provider
        model = self.config.chat_model
        cmd = [self.config.command_for(provider)]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["--print", prompt])
        working_dir = self.projects[0].working_directory
        res = runner.run(cmd, cwd=working_dir, timeout_seconds=10)
        if res.return_code == 0 and res.stdout:
            ans = res.stdout.strip()
            for p in self.projects:
                if p.name.lower() == ans.lower():
                    return p
        return None

    def _find_project(self, name: str | None) -> ProjectScope | None:
        if not name:
            return None
        for p in self.projects:
            if p.name == name or p.workspace_id == name:
                return p
        return None
