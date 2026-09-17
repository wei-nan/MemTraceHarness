from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from memtrace_harness.adapters.cli import CliModelAdapter
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.schemas import TaskEnvelope, TokenUsage


class ClaudeCliAdapter(CliModelAdapter):
    provider = "claude"
    prompt_via_stdin = True

    def __init__(self, *, executable: str = "claude", **kwargs: Any) -> None:
        super().__init__(executable=executable, **kwargs)

    def build_command(self, prompt: str) -> list[str]:
        command = [
            self.executable,
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(["--effort", self.reasoning_effort])
        if self.output_schema_path:
            schema = json.loads(self.output_schema_path.read_text(encoding="utf-8"))
            command.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
        # 2026-09-05: --permission-mode "default" (the read-only branch below)
        # requires per-tool approval for anything not pre-allowlisted, and there's
        # no human present headlessly to grant it — a G1 Red Team run observed
        # this denying its own get_node call outright ("被拒"), not a considered
        # choice not to search. MemTrace's own read-only lookup tools are always
        # pre-allowlisted below so every stage can actually query MemTrace, not
        # just be told in its prompt that it's allowed to. write_tools stays empty
        # for every role except Controller (see controller_task()'s converge-stage
        # instruction to record completion) — no other role should get standing
        # permission to alter the KB.
        read_tools = "mcp__memtrace__search_nodes,mcp__memtrace__get_node,mcp__memtrace__list_nodes,mcp__memtrace__traverse"
        write_tools = (
            ",mcp__memtrace__create_node,mcp__memtrace__update_node"
            if self.role_profile_id == "controller"
            else ""
        )
        if self.permission == "read-only":
            # NOT --permission-mode plan: that mode is built for an interactive human
            # session (it can hand off to ExitPlanMode, or — observed 2026-08-12 on a
            # real Red Team run — fall back to writing the model's full analysis and
            # JSON verdict into a ~/.claude/plans/*.md file instead of returning it as
            # the turn's final text). Headlessly, Harness never sees that file; it
            # only sees a prose "I wrote the plan to a file" summary, which fails the
            # "final text is exactly one JSON object" contract every stage relies on.
            # --permission-mode default + explicitly disallowing the write-capable
            # tools gets the same "cannot modify the repo" guarantee without route-ing
            # through that plan-mode UX. Read-only tool use (Bash for grep-style
            # queries, Read, etc.) is unaffected.
            command.extend(
                [
                    "--permission-mode",
                    "default",
                    "--disallowedTools",
                    "Write,Edit,NotebookEdit",
                    "--allowedTools",
                    read_tools + write_tools,
                ]
            )
        else:
            command.extend(
                ["--permission-mode", "acceptEdits", "--allowedTools", read_tools + write_tools]
            )
        return command

    def parse_response(
        self,
        task: TaskEnvelope,
        process: ProcessResult,
        events: list[dict[str, Any]],
    ):
        result = next((event for event in reversed(events) if event.get("type") == "result"), {})
        usage_data = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        has_usage = bool(usage_data)
        usage = TokenUsage(
            input_tokens=_int(usage_data.get("input_tokens")),
            cached_input_tokens=_int(usage_data.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_int(usage_data.get("cache_creation_input_tokens")),
            output_tokens=_int(usage_data.get("output_tokens")),
            total_tokens=_optional_int(usage_data.get("total_tokens")),
            cost_usd=_optional_float(result.get("total_cost_usd")),
            completeness="complete" if has_usage else "unavailable",
        )
        final_text = (
            _json_text(result.get("structured_output"))
            or _json_text(result.get("structuredOutput"))
            or _json_text(result.get("result"))
            or _last_text(events)
            or _plain_fallback(process.stdout)
        )
        provider_run_id = _optional_str(result.get("session_id")) or _last_value(
            events, "session_id"
        )
        warnings = [] if has_usage else ["Claude result event did not include usage"]
        response = self.empty_response(
            task=task,
            final_text=final_text,
            usage=usage,
            provider_run_id=provider_run_id,
            warnings=warnings,
        )
        resolved_model = _optional_str(result.get("model"))
        if resolved_model:
            response = replace(
                response,
                execution=replace(response.execution, resolved_model=resolved_model),
            )
        return response


def _last_text(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if texts:
            return "\n".join(str(text) for text in texts)
    return None


def _last_value(events: list[dict[str, Any]], key: str) -> str | None:
    for event in reversed(events):
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


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    return None if value is None else _int(value)


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)
