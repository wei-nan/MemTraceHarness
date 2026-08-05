from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig
    from memtrace_harness.scope import ProjectScope


@dataclass
class TriageResult:
    kind: str  # "approval_response", "task", "status", "out_of_scope", "unrecognized"
    project_scope: ProjectScope | None
    approval_id: str | None = None
    approval_action: str | None = None  # "approve", "reject", "clarify"
    approval_reason: str | None = None
    task_goal: str | None = None
    rejection_message: str | None = None


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
                    rejection_message=f"Command {parts[0]} requires an approval ID (e.g. {parts[0]} appr_123456)",
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

        # Check for scope/boundary violations
        lower_text = cleaned.lower()
        if any(keyword in lower_text for keyword in ["open a pr", "pull request", "check my email", "send email"]):
            return TriageResult(
                kind="out_of_scope",
                project_scope=None,
                rejection_message="Request is out of Harness remote operations scope (e.g. GitHub PRs and external services beyond git push are unsupported).",
            )

        # Match project
        matched_project = self._match_project_from_text(cleaned) or self._find_project(default_project)
        if not matched_project and len(self.projects) == 1:
            matched_project = self.projects[0]

        if not matched_project:
            return TriageResult(
                kind="unrecognized",
                project_scope=None,
                rejection_message="Could not resolve request to a registered project scope (no harness-scope.md found).",
            )

        # Check off-limits rules in matched_project
        if matched_project.off_limits:
            for rule in matched_project.off_limits:
                if rule.lower() in lower_text:
                    return TriageResult(
                        kind="out_of_scope",
                        project_scope=matched_project,
                        rejection_message=f"Request touches off-limits rule specified in harness-scope.md: '{rule}'",
                    )

        if lower_text in {"status", "help", "/status", "/help"}:
            return TriageResult(kind="status", project_scope=matched_project)

        return TriageResult(
            kind="task",
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
