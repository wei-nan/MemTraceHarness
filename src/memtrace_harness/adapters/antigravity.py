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
        command = [self.executable]
        if self.structured_output:
            command.extend(["--output-format", "stream-json"])
        if self.model:
            command.extend(["--model", self.model])
        # Some Antigravity model names already bake an effort tier into the name itself
        # (e.g. "gemini-3.6-flash-high", "gpt-oss-120b-medium") — passing --effort
        # alongside one of these is a hard CLI error ("--model X conflicts with
        # --effort=Y"), not a warning.
        if self.reasoning_effort and not _model_has_builtin_effort(self.model):
            command.extend(["--effort", self.reasoning_effort])
        if self.output_schema_path:
            command.extend(["--json-schema", str(self.output_schema_path)])
        # Always accept-edits, even for read-only roles: --mode plan is semantically
        # correct (it never writes) but structurally requires an interactive "Proceed"
        # confirmation with no headless bypass — confirmed even with
        # --dangerously-skip-permissions, so it simply never completes unattended (see
        # the antigravity-headless-command-permission memory note). Plan mode's actual
        # safety property — a read-only role never truly writes — is no longer this
        # flag's job: CliModelAdapter.run() in cli.py independently diffs git status
        # before/after any permission="read-only" call and fails closed if anything
        # changed, regardless of provider or CLI mode. That backstop is what makes it
        # safe to stop depending on Antigravity's own (non-functional, headlessly) gate.
        command.extend(["--mode", "accept-edits"])
        # --project does NOT bind the working directory despite its name/description
        # (verified empirically: commands still ran against agy's own install dir).
        # --add-dir is what actually does. --sandbox was dropped: every successful
        # manual verification this session omitted it, and it's undetermined whether it
        # contributed to the workdir/prompt-delivery failures seen before this fix.
        command.extend(["--add-dir", str(self.working_directory)])
        # --prompt "<text>" (the flag's own value) must come last: `--print` (bare) with
        # the prompt as a trailing positional does not reliably deliver it to the model
        # (verified empirically, 2026-08-12 — it fell back to answering generic questions
        # about its own flags instead). This also keeps the prompt as the command list's
        # last element, which redact_command() in cli.py assumes when scrubbing traces.
        command.extend(["--prompt", prompt])
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
            error=_last_error(events),
        )
        resolved_model = _last_model(events)
        if resolved_model:
            response = replace(
                response,
                execution=replace(response.execution, resolved_model=resolved_model),
            )
        return response


def _model_has_builtin_effort(model: str | None) -> bool:
    if not model:
        return False
    suffix = model.rsplit("-", 1)[-1].lower()
    # "-high"/"-medium"/"-low" (e.g. gemini-3.6-flash-high) bake in an effort tier.
    # "-thinking" (e.g. claude-opus-4-6-thinking) isn't an effort tier at all — it's a
    # fixed reasoning mode — but the CLI rejects --effort for it just the same
    # ("--effort is not supported for model ..."), confirmed directly against `agy`
    # 2026-08-13. Same fix, same reasoning: don't pass a flag the CLI will reject for
    # this specific model name shape.
    return suffix in {"high", "medium", "low", "thinking"}


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


def _last_error(events: list[dict[str, Any]]) -> str | None:
    """Antigravity's stream-json protocol reports errors (quota exhaustion, safety
    refusals, subagent cancellation, etc.) as data inside a normal `{"event":"result"}`
    JSON line, not necessarily via a nonzero process exit code — confirmed 2026-08-14:
    a real quota-exhaustion mid-call ("Individual quota reached... Resets in
    4h33m10s") returned exit code 0 with a "status":"ERROR" result event, which this
    adapter previously ignored entirely, so the failure surfaced as a generic schema
    validation error instead of the quota_exhausted category it actually was —
    silently defeating the loop's own cross-provider fallback (which only triggers for
    quota/rate-limit/overload categories). Surfacing this here lets
    classify_execution_failure() in fallback.py see the real error text and
    CliModelAdapter.run() in cli.py override status to "failed" even when the process
    exit code alone would say "succeeded"."""
    for event in reversed(events):
        result = event.get("result") if isinstance(event.get("result"), dict) else None
        for container in (result, event):
            if not isinstance(container, dict):
                continue
            if container.get("status") == "ERROR" or container.get("error"):
                error = container.get("error")
                if isinstance(error, str) and error.strip():
                    return error.strip()
                return "Antigravity reported an error result with no error message"
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
