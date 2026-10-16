from __future__ import annotations

from dataclasses import replace
from typing import Any

from memtrace_harness.adapters.cli import CliModelAdapter
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.schemas import TaskEnvelope, TokenUsage


class CursorCliAdapter(CliModelAdapter):
    """Cursor's headless agent (`cursor-agent -p`). Verified against 2026.10.14 on 2026-10-14.

    Permission mapping (probed, not assumed):
      * read-only      -> ``--mode ask``: reads files, refuses to write or run commands. (``--mode plan``
                          is not used: it answers "here is my plan" instead of doing the stage, the same
                          way Claude's plan mode diverted a verdict — see claude_plan_mode_json_diversion.)
      * workspace-write -> ``--force``: without it ``-p`` can edit files but every shell command is
                          denied, and a develop stage must run tests and git.
    The read-only guarantee does not rest on ``--mode ask`` alone: CliModelAdapter.run() diffs git
    status around every read-only call and fails closed if it changed.

    The CLI has no schema flag, so a role's output schema is appended to the prompt instead.
    Failures arrive two ways: a hard failure (bad model, not logged in) prints plain text and exits 1
    with nothing on stdout; a model-side one is a ``result`` event with ``is_error`` true."""

    provider = "cursor"

    def __init__(self, *, executable: str = "cursor-agent", **kwargs: Any) -> None:
        super().__init__(executable=executable, **kwargs)

    def build_command(self, prompt: str) -> list[str]:
        command = [
            self.executable,
            "--print",
            "--output-format",
            "stream-json",
            "--trust",
            "--workspace",
            str(self.working_directory),
        ]
        if self.model:
            # Cursor bakes the reasoning tier into the model name (gpt-5.6-sol-high,
            # claude-sonnet-5-thinking-xhigh), so reasoning_effort has no flag to travel on.
            command.extend(["--model", self.model])
        if self.permission == "read-only":
            command.extend(["--mode", "ask"])
        else:
            command.append("--force")
        if self.permission == "read-only":
            for directory in self.read_directories:
                command.extend(["--add-dir", str(directory)])
        else:
            for directory in self.write_directories:
                command.extend(["--add-dir", str(directory)])
        # The prompt stays last: redact_command() in cli.py scrubs the last element from traces.
        command.append(self._with_output_schema(prompt))
        return command

    def _with_output_schema(self, prompt: str) -> str:
        if not self.output_schema_path:
            return prompt
        try:
            schema = self.output_schema_path.read_text(encoding="utf-8").strip()
        except OSError:
            return prompt
        return (
            f"{prompt}\n\nYour final message must be exactly one JSON object that validates against this "
            f"JSON Schema, with no prose and no code fence around it:\n{schema}"
        )

    def parse_response(
        self,
        task: TaskEnvelope,
        process: ProcessResult,
        events: list[dict[str, Any]],
    ):
        result = _result_event(events)
        usage_data = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        usage = TokenUsage(
            input_tokens=_int(usage_data.get("inputTokens")),
            cached_input_tokens=_int(usage_data.get("cacheReadTokens")),
            output_tokens=_int(usage_data.get("outputTokens")),
            # "partial", not "complete": the stream does not say whether inputTokens already counts
            # the cache-read tokens, so a total would be a guess.
            completeness="partial" if usage_data else "unavailable",
        )
        final_text = _final_text(events, result) or _plain_fallback(process)
        warnings = []
        if usage_data:
            warnings.append(
                "Cursor usage does not say whether inputTokens includes cacheReadTokens; "
                "raw trace is authoritative"
            )
        else:
            warnings.append("Cursor output did not include a result usage object")
        response = self.empty_response(
            task=task,
            final_text=final_text,
            usage=usage,
            provider_run_id=_session_id(events),
            warnings=warnings,
            error=_error(result, process),
        )
        resolved_model = _resolved_model(events)
        if resolved_model:
            response = replace(
                response,
                execution=replace(response.execution, resolved_model=resolved_model),
            )
        return response


def _result_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return {}


def _final_text(events: list[dict[str, Any]], result: dict[str, Any]) -> str | None:
    text = result.get("result")
    if isinstance(text, str) and text.strip():
        return text
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            parts = [
                str(item["text"])
                for item in content
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
            ]
            if parts:
                return "".join(parts)
    return None


def _error(result: dict[str, Any], process: ProcessResult) -> str | None:
    """Surface a failure as text so fallback.py can classify it (quota, rate limit, authentication...).
    An is_error result can carry exit code 0, and a hard failure prints plain text on stderr, not JSON."""
    if result.get("is_error") or result.get("subtype") == "error":
        detail = result.get("result") or result.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:500]
        return "Cursor reported an error result with no error message"
    if not result and process.return_code not in (None, 0):
        lines = [line for line in (process.stderr or process.stdout).strip().splitlines() if line.strip()]
        if lines:
            return lines[0][:500]
    return None


def _session_id(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("session_id"):
            return str(event["session_id"])
    return None


def _resolved_model(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "system" and event.get("model"):
            return str(event["model"])
    return None


def _plain_fallback(process: ProcessResult) -> str:
    stdout = process.stdout.strip()
    return stdout if stdout and not stdout.lstrip().startswith("{") else ""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
