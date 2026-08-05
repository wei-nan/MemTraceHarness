from __future__ import annotations

from abc import abstractmethod
from dataclasses import replace
import json
from pathlib import Path
import re
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

    def run(self, task: TaskEnvelope, trace_id: str) -> ModelResponse:
        prompt = render_agent_prompt(task, self.role)
        command = self.build_command(prompt)
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
            else "succeeded"
            if process.return_code == 0
            else "failed"
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
            error=process.error or _stderr_error(process),
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


def render_agent_prompt(task: TaskEnvelope, role: str) -> str:
    payload = task.to_dict()
    return "\n".join(
        [
            f"You are the {role} in a MemTrace external harness run.",
            "Execute the goal in the current working directory when edits are requested.",
            "Follow repository agent instructions and preserve human approval checkpoints.",
            "Do not call a model provider API or reveal credentials.",
            (
                "End with a concise result containing evidence, tests, gaps, and human "
                "decisions needed."
            ),
            "Task envelope:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


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
