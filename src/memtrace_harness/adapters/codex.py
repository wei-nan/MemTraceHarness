from __future__ import annotations

from dataclasses import replace
from typing import Any

from memtrace_harness.adapters.cli import CliModelAdapter
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.schemas import TaskEnvelope, TokenUsage


class CodexCliAdapter(CliModelAdapter):
    provider = "codex"
    prompt_via_stdin = True

    def __init__(self, *, executable: str = "codex", **kwargs: Any) -> None:
        super().__init__(executable=executable, **kwargs)

    def build_command(self, prompt: str) -> list[str]:
        command = [self.executable, "exec", "--json"]
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(
                ["--config", f'model_reasoning_effort="{self.reasoning_effort}"']
            )
        if self.output_schema_path:
            command.extend(["--output-schema", str(self.output_schema_path)])
        command.extend(["--sandbox", self.permission])
        if self.context_policy in {"loop-snapshot", "gate-evidence-only"}:
            command.append("--ignore-user-config")
        if self.context_policy == "loop-snapshot":
            command.extend(["--ignore-rules", "--skip-git-repo-check"])
        command.append("-")
        return command

    def parse_response(
        self,
        task: TaskEnvelope,
        process: ProcessResult,
        events: list[dict[str, Any]],
    ):
        completed_turns = [event for event in events if event.get("type") == "turn.completed"]
        usage_dicts = [
            event.get("usage")
            for event in completed_turns
            if isinstance(event.get("usage"), dict)
        ]
        usage = TokenUsage(
            input_tokens=sum(_int(item.get("input_tokens")) for item in usage_dicts),
            cached_input_tokens=sum(_int(item.get("cached_input_tokens")) for item in usage_dicts),
            output_tokens=sum(_int(item.get("output_tokens")) for item in usage_dicts),
            reasoning_output_tokens=sum(
                _int(item.get("reasoning_output_tokens")) for item in usage_dicts
            ),
            total_tokens=_sum_optional(usage_dicts, "total_tokens"),
            completeness="complete" if usage_dicts else "unavailable",
        )
        final_text = _last_agent_message(events) or _plain_fallback(process.stdout)
        thread = next((event for event in events if event.get("type") == "thread.started"), {})
        provider_run_id = str(thread.get("thread_id")) if thread.get("thread_id") else None
        warnings = [] if usage_dicts else ["Codex turn.completed event did not include usage"]
        response = self.empty_response(
            task=task,
            final_text=final_text,
            usage=usage,
            provider_run_id=provider_run_id,
            warnings=warnings,
            error=_last_error_message(events),
        )
        resolved_model = _last_model(events)
        if resolved_model:
            response = replace(
                response,
                execution=replace(response.execution, resolved_model=resolved_model),
            )
        return response


def _last_agent_message(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            return str(item["text"])
    return None


def _last_error_message(events: list[dict[str, Any]]) -> str | None:
    """Codex reports a failed turn as a JSON line on stdout, not stderr — e.g.
    {"type":"error","message":"You've hit your usage limit..."} or a
    {"type":"turn.failed","error":{"message":...}} event. Without this, a real quota/
    rate-limit message is invisible to failure classification and every failure looks
    like an unrecognized "unknown" error, which fails closed instead of falling back."""
    for event in reversed(events):
        event_type = event.get("type")
        if event_type == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
        elif event_type == "error" and event.get("message"):
            return str(event["message"])
    return None


def _plain_fallback(stdout: str) -> str:
    return stdout.strip() if stdout.strip() and not stdout.lstrip().startswith("{") else ""


def _last_model(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("model"):
            return str(event["model"])
    return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sum_optional(items: list[dict[str, Any]], key: str) -> int | None:
    values = [item.get(key) for item in items if item.get(key) is not None]
    return sum(_int(value) for value in values) if values else None
