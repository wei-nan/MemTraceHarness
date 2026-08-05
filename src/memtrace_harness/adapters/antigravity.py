from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from memtrace_harness.adapters.cli import CliModelAdapter
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.schemas import TaskEnvelope, TokenUsage


class AntigravityCliAdapter(CliModelAdapter):
    provider = "antigravity"

    def __init__(
        self, *, executable: str = "agy", structured_output: bool = False, **kwargs: Any
    ) -> None:
        self.structured_output = structured_output
        self.expects_json_lines = structured_output
        super().__init__(executable=executable, **kwargs)

    def build_command(self, prompt: str) -> list[str]:
        command = [self.executable, "--print"]
        if self.structured_output:
            command.extend(["--output-format", "stream-json"])
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(["--effort", self.reasoning_effort])
        if self.output_schema_path:
            command.extend(["--json-schema", str(self.output_schema_path)])
        command.extend(
            ["--mode", "plan" if self.permission == "read-only" else "accept-edits"]
        )
        command.extend(["--sandbox", prompt])
        return command

    def parse_response(
        self,
        task: TaskEnvelope,
        process: ProcessResult,
        events: list[dict[str, Any]],
    ):
        usage_data = _last_usage(events)
        has_usage = bool(usage_data)
        usage = TokenUsage(
            input_tokens=_first_int(usage_data, "input_tokens", "inputTokens", "prompt_tokens"),
            cached_input_tokens=_first_int(
                usage_data, "cached_input_tokens", "cachedInputTokens", "cache_read_input_tokens"
            ),
            output_tokens=_first_int(
                usage_data, "output_tokens", "outputTokens", "completion_tokens"
            ),
            reasoning_output_tokens=_first_int(
                usage_data, "reasoning_output_tokens", "reasoningTokens", "thoughts_tokens"
            ),
            total_tokens=_first_optional_int(usage_data, "total_tokens", "totalTokens"),
            cost_usd=_first_optional_float(usage_data, "cost_usd", "total_cost_usd"),
            completeness="partial" if has_usage else "unavailable",
        )
        final_text = _last_text(events) or _plain_fallback(process.stdout)
        provider_run_id = _last_id(events)
        warnings = []
        if self.structured_output:
            warnings.append(
                "Antigravity stream-json usage schema is treated as version-sensitive; "
                "raw trace is authoritative"
            )
        else:
            warnings.append(
                "Installed Antigravity CLI does not advertise stream-json; usage is "
                "unavailable in text mode"
            )
        if not has_usage:
            warnings.append("Antigravity output did not expose a recognized usage object")
        response = self.empty_response(
            task=task,
            final_text=final_text,
            usage=usage,
            provider_run_id=provider_run_id,
            warnings=warnings,
        )
        resolved_model = _last_model(events)
        if resolved_model:
            response = replace(
                response,
                execution=replace(response.execution, resolved_model=resolved_model),
            )
        return response


def _last_usage(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        for key in ("usage", "usage_metadata", "usageMetadata"):
            value = event.get(key)
            if isinstance(value, dict):
                return value
        result = event.get("result")
        if isinstance(result, dict):
            for key in ("usage", "usage_metadata", "usageMetadata"):
                value = result.get(key)
                if isinstance(value, dict):
                    return value
    return {}


def _last_text(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        containers = [event]
        if isinstance(event.get("result"), dict):
            containers.append(event["result"])
        for container in containers:
            for key in ("structured_output", "structuredOutput", "response", "text"):
                value = _json_text(container.get(key))
                if value:
                    return value
            if container is event:
                value = _json_text(event.get("result"))
                if value and not isinstance(event.get("result"), dict):
                    return value
            message = container.get("message")
            if isinstance(message, dict):
                value = _json_text(message.get("content"))
                if value:
                    return value
    return None


def _last_id(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
            if event.get(key):
                return str(event[key])
    return None


def _last_model(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        for key in ("model", "model_id", "modelId"):
            if event.get(key):
                return str(event[key])
    return None


def _plain_fallback(stdout: str) -> str:
    return stdout.strip() if stdout.strip() and not stdout.lstrip().startswith("{") else ""


def _json_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return None


def _first_int(data: dict[str, Any], *keys: str) -> int:
    value = _first(data, *keys)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_optional_int(data: dict[str, Any], *keys: str) -> int | None:
    value = _first(data, *keys)
    return None if value is None else _first_int(data, *keys)


def _first_optional_float(data: dict[str, Any], *keys: str) -> float | None:
    value = _first(data, *keys)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None
