from __future__ import annotations

from abc import abstractmethod
from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.cli_process import CliProcessRunner, ProcessResult
from memtrace_harness.schemas import (
    CliExecution,
    ModelClaim,
    ModelResponse,
    ProviderId,
    TaskEnvelope,
    TokenUsage,
)


class CliModelAdapter(ModelAdapter):
    provider: ProviderId
    prompt_via_stdin = False
    expects_json_lines = True

    def __init__(
        self,
        *,
        adapter_id: str,
        role: str,
        executable: str,
        working_directory: Path,
        trace_root: Path,
        timeout_seconds: int,
        process_runner: CliProcessRunner | None = None,
        role_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        permission: str = "workspace-write",
        context_policy: str = "task-and-targeted-evidence",
        cli_version: str | None = None,
        output_schema_path: Path | None = None,
        quota_bucket: str | None = None,
        fallback_index: int = 0,
        reply_language: str | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.role = role
        self.executable = executable
        self.working_directory = working_directory
        self.trace_root = trace_root
        self.timeout_seconds = timeout_seconds
        self.process_runner = process_runner or CliProcessRunner()
        self.role_profile_id = role_profile_id
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.permission = permission
        self.context_policy = context_policy
        self.cli_version = cli_version
        self.output_schema_path = output_schema_path
        self.quota_bucket = quota_bucket or f"{self.provider}-account"
        self.fallback_index = fallback_index
        self.reply_language = reply_language

    def run(self, task: TaskEnvelope, trace_id: str) -> ModelResponse:
        prompt = render_agent_prompt(task, self.role, reply_language=self.reply_language)
        command = self.build_command(prompt)
        # For a read-only role, this is the one independent check that doesn't trust
        # the CLI's own mode gate — some providers (Antigravity's --mode plan) can't
        # complete headlessly, and the only way to still use them for a read-only role
        # is a mode that's technically capable of writing (accept-edits). Comparing
        # git state before/after means a write is caught and fails closed even if the
        # CLI-level permission gate was bypassed or never enforced it in the first
        # place — see the antigravity-headless-command-permission memory note.
        before_snapshot = _git_status_snapshot(self.working_directory) if self.permission == "read-only" else None
        process = self.process_runner.run(
            command,
            cwd=self.working_directory,
            timeout_seconds=self.timeout_seconds,
            input_text=prompt if self.prompt_via_stdin else None,
        )
        raw_trace_ref, stderr_ref = self._write_raw_trace(trace_id, process)
        events, parse_warnings = (
            parse_json_lines(process.stdout) if self.expects_json_lines else ([], [])
        )
        response = self.parse_response(task, process, events)
        status = (
            "unavailable"
            if process.unavailable
            else "timed_out"
            if process.timed_out
            # A provider CLI can report an internal error (quota exhaustion, safety
            # refusal, subagent cancellation) as data inside its own JSON protocol
            # while still exiting 0 — process.return_code alone would misreport this
            # as "succeeded" (confirmed 2026-08-14 for Antigravity's quota errors; see
            # adapters/antigravity.py's _last_error()). If the adapter's own
            # parse_response() surfaced an error, trust that over a clean exit code.
            else "failed"
            if response.execution.error
            else "succeeded"
            if process.return_code == 0
            else "failed"
        )
        error = process.error or response.execution.error or _stderr_error(process)
        if before_snapshot is not None:
            after_snapshot = _git_status_snapshot(self.working_directory)
            if after_snapshot is not None and after_snapshot != before_snapshot:
                # Fail closed, don't try to auto-revert: an automatic git cleanup here
                # could itself destroy legitimate uncommitted work if this check ever
                # has a bug. Surfacing it loudly and stopping is the safe choice; a
                # human decides what to do with the actual diff.
                status = "failed"
                error = (
                    f"safety violation: a read-only role ({self.role_profile_id or self.role}) "
                    "modified the working directory despite permission=read-only. Working "
                    "tree state changed during this call — treating the result as untrusted "
                    "and stopping instead of using it. Inspect `git status`/`git diff` in "
                    f"{self.working_directory} before deciding how to proceed."
                )
        execution = replace(
            response.execution,
            status=status,
            command=redact_command(
                process.command, contains_prompt=not self.prompt_via_stdin
            ),
            exit_code=process.return_code,
            started_at=process.started_at,
            completed_at=process.completed_at,
            duration_ms=process.duration_ms,
            raw_trace_ref=raw_trace_ref,
            stderr_ref=stderr_ref,
            # process.error only sees subprocess-launch failures. response.execution.error
            # (set by parse_response(), if the adapter surfaces one) is preferred over
            # _stderr_error() because a provider CLI can report its failure as a JSON line
            # on stdout instead of stderr (e.g. Codex's `{"type":"error","message":...}`);
            # _stderr_error() falls back to a generic "CLI exited with code N" when stderr
            # is empty, which would otherwise silently win and discard the real message.
            error=error,
            parse_warnings=[*response.execution.parse_warnings, *parse_warnings],
        )
        return replace(response, execution=execution)

    @abstractmethod
    def build_command(self, prompt: str) -> list[str]:
        """Return argv for one non-interactive provider CLI turn."""

    @abstractmethod
    def parse_response(
        self,
        task: TaskEnvelope,
        process: ProcessResult,
        events: list[dict[str, Any]],
    ) -> ModelResponse:
        """Normalize provider-specific final output and usage."""

    def _write_raw_trace(self, trace_id: str, process: ProcessResult) -> tuple[str, str | None]:
        trace_dir = self.trace_root / trace_id
        trace_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_name(self.adapter_id)
        stdout_path = trace_dir / f"{stem}.stdout.log"
        stderr_path = trace_dir / f"{stem}.stderr.log"
        stdout_path.write_text(process.stdout, encoding="utf-8")
        stderr_ref = None
        if process.stderr:
            stderr_path.write_text(process.stderr, encoding="utf-8")
            stderr_ref = str(stderr_path)
        return str(stdout_path), stderr_ref

    def empty_response(
        self,
        *,
        task: TaskEnvelope,
        final_text: str,
        usage: TokenUsage,
        provider_run_id: str | None,
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> ModelResponse:
        claim = _claim_from_final_text(final_text, task)
        return ModelResponse(
            adapter_id=self.adapter_id,
            role=self.role,
            claims=[claim] if claim else [],
            disagreements=[],
            requires_human_decision=True,
            final_text=final_text,
            execution=CliExecution(
                provider=self.provider,
                status="failed",
                command=[],
                exit_code=None,
                started_at="",
                completed_at="",
                duration_ms=0,
                usage=usage,
                provider_run_id=provider_run_id,
                parse_warnings=warnings or [],
                error=error,
                role_profile_id=self.role_profile_id,
                requested_model=self.model,
                reasoning_effort=self.reasoning_effort,
                permission=self.permission,
                context_policy=self.context_policy,
                cli_version=self.cli_version,
                quota_bucket=self.quota_bucket,
                fallback_index=self.fallback_index,
            ),
        )


def _git_status_snapshot(working_directory: Path) -> str | None:
    """Cheap, dependency-free proof of "did anything in the working tree change":
    `git status --porcelain` output for tracked and untracked files, plus the
    current HEAD (catches a commit with an otherwise-clean tree). Returns None when
    working_directory isn't a git repo at all (e.g. Controller's isolated sandbox
    directory under trace_root/controller-workspace, which is intentionally not a
    real project checkout) — nothing to protect there, so the caller skips the
    write-detection check entirely rather than treating "not a repo" as a violation."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode != 0:
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return f"{head.stdout.strip()}\n{status.stdout}"
    except (OSError, subprocess.SubprocessError):
        return None


def render_agent_prompt(
    task: TaskEnvelope, role: str, *, reply_language: str | None = None
) -> str:
    payload = task.to_dict()
    lines = [
        f"You are the {role} in a MemTrace external harness run.",
        "Execute the goal in the current working directory when edits are requested.",
        "Follow repository agent instructions and preserve human approval checkpoints.",
        "Do not call a model provider API or reveal credentials.",
        (
            "End with a concise result containing evidence, tests, gaps, and human "
            "decisions needed."
        ),
    ]
    if reply_language:
        # A fixed, Harness-owned policy — not derived from task content, so it never
        # grows or duplicates across resume rounds the way baking it into `goal` text
        # did (see the resume_goal-augmentation-nesting fix). Every role gets exactly
        # this one line, once, regardless of how many times a conversation resumes.
        lines.append(
            f"Reply/summarize in {reply_language}, regardless of what language the "
            "task envelope or prior discussion uses."
        )
    lines.extend(
        [
            "Task envelope:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )
    return "\n".join(lines)


def parse_json_lines(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    warnings: list[str] = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"stdout line {number} was not JSON")
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            warnings.append(f"stdout line {number} was JSON but not an object")
    return events, warnings


def redact_command(command: list[str], *, contains_prompt: bool) -> list[str]:
    if not command:
        return []
    redacted = list(command)
    if contains_prompt and len(redacted) > 1:
        redacted[-1] = "<task-prompt>"
    return redacted


def _claim_from_final_text(final_text: str, task: TaskEnvelope) -> ModelClaim | None:
    text = final_text.strip()
    if not text:
        return None
    return ModelClaim(
        claim=text[:2000],
        evidence_refs=task.context_refs[:10],
        confidence=0.5,
        risk=task.risk_level,
        proposed_next_action="Require human review before canonical knowledge adoption.",
    )


def _stderr_error(process: ProcessResult) -> str | None:
    if process.return_code in (None, 0):
        return None
    detail = process.stderr.strip().splitlines()
    return detail[-1][:500] if detail else f"CLI exited with code {process.return_code}"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)
