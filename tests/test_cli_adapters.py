from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.adapters import (
    AntigravityCliAdapter,
    ClaudeCliAdapter,
    CodexCliAdapter,
)
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.output_contracts import output_schema_path
from memtrace_harness.schemas import TaskEnvelope


class StaticProcessRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], Path, int, str | None]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> ProcessResult:
        self.calls.append((command, cwd, timeout_seconds, input_text))
        return replace(self.result, command=command)


def process_result(stdout: str, *, return_code: int | None = 0, **kwargs) -> ProcessResult:
    return ProcessResult(
        command=[],
        return_code=return_code,
        stdout=stdout,
        stderr=kwargs.pop("stderr", ""),
        started_at="2026-08-03T00:00:00+00:00",
        completed_at="2026-08-03T00:00:01+00:00",
        duration_ms=1000,
        **kwargs,
    )


class CliAdapterTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir_context = TemporaryDirectory()
        self.addCleanup(self.temp_dir_context.cleanup)
        self.root = Path(self.temp_dir_context.name)
        self.task = TaskEnvelope(
            task_id="task_test",
            workspace_id="ws_spec_plan",
            goal="Inspect the repository",
            context_refs=["mem_boundary"],
        )

    def test_claude_stream_usage_is_normalized(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "session_id": "session_claude"}),
                json.dumps(
                    {
                        "type": "result",
                        "session_id": "session_claude",
                        "result": "Claude completed the task.",
                        "total_cost_usd": 0.12,
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "cache_creation_input_tokens": 3,
                            "output_tokens": 4,
                        },
                    }
                ),
            ]
        )
        runner = StaticProcessRunner(process_result(stdout))
        adapter = self._adapter(ClaudeCliAdapter, runner)

        response = adapter.run(self.task, "run_claude")

        self.assertEqual(response.execution.status, "succeeded")
        self.assertEqual(response.execution.provider_run_id, "session_claude")
        self.assertEqual(response.execution.usage.input_tokens, 10)
        self.assertEqual(response.execution.usage.cached_input_tokens, 20)
        self.assertEqual(response.execution.usage.output_tokens, 4)
        self.assertEqual(response.execution.usage.completeness, "complete")
        self.assertEqual(response.final_text, "Claude completed the task.")
        self.assertEqual(
            runner.calls[0][0][0:4],
            ["tool", "--print", "--output-format", "stream-json"],
        )
        self.assertNotIn("<task-prompt>", response.execution.command)
        self.assertIn("Inspect the repository", runner.calls[0][3])
        self.assertTrue(Path(response.execution.raw_trace_ref).exists())

    def test_codex_turn_usage_is_summed(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_codex"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Codex completed the task."},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 30,
                            "cached_input_tokens": 11,
                            "output_tokens": 7,
                            "reasoning_output_tokens": 5,
                        },
                    }
                ),
            ]
        )
        adapter = self._adapter(CodexCliAdapter, StaticProcessRunner(process_result(stdout)))

        response = adapter.run(self.task, "run_codex")

        self.assertEqual(response.execution.provider_run_id, "thread_codex")
        self.assertEqual(response.execution.usage.input_tokens, 30)
        self.assertEqual(response.execution.usage.cached_input_tokens, 11)
        self.assertEqual(response.execution.usage.reasoning_output_tokens, 5)
        self.assertEqual(response.execution.usage.completeness, "complete")
        self.assertEqual(
            response.execution.command[1:5], ["exec", "--json", "--sandbox", "workspace-write"]
        )
        self.assertEqual(response.execution.command[-1], "-")

    def test_codex_usage_limit_error_is_surfaced_for_failure_classification(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_codex"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {
                        "type": "error",
                        "message": "You've hit your usage limit. Try again at Aug 8th, 2026 9:47 PM.",
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {
                            "message": "You've hit your usage limit. Try again at Aug 8th, 2026 9:47 PM."
                        },
                    }
                ),
            ]
        )
        adapter = self._adapter(
            CodexCliAdapter, StaticProcessRunner(process_result(stdout, return_code=1))
        )

        response = adapter.run(self.task, "run_codex")

        # A CLI failure reported as a stdout JSON line (not stderr) must still reach
        # execution.error, otherwise failure classification sees no text at all, calls
        # it "unknown", and the harness fails closed instead of falling back providers.
        self.assertEqual(response.execution.status, "failed")
        self.assertIn("usage limit", response.execution.error or "")

    def test_claude_structured_output_object_is_normalized_to_json_text(self) -> None:
        artifact = {
            "status": "ready",
            "plan": "bounded plan",
            "acceptance_criteria": ["observable"],
            "open_questions": [],
            "scope_exclusions": [],
        }
        stdout = json.dumps(
            {
                "type": "result",
                "session_id": "session_structured",
                "structured_output": artifact,
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }
        )
        adapter = self._adapter(
            ClaudeCliAdapter,
            StaticProcessRunner(process_result(stdout)),
        )

        response = adapter.run(self.task, "run_structured_claude")

        self.assertEqual(json.loads(response.final_text), artifact)

    def test_antigravity_nested_structured_output_is_normalized_to_json_text(self) -> None:
        artifact = {
            "status": "completed",
            "summary": "done",
            "changed_files": [],
            "tests": [],
            "gaps": [],
        }
        stdout = json.dumps(
            {
                "type": "result",
                "result": {
                    "structuredOutput": artifact,
                    "usageMetadata": {"inputTokens": 7, "outputTokens": 4},
                },
            }
        )
        adapter = self._adapter(
            AntigravityCliAdapter,
            StaticProcessRunner(process_result(stdout)),
            structured_output=True,
        )

        response = adapter.run(self.task, "run_structured_antigravity")

        self.assertEqual(json.loads(response.final_text), artifact)
        self.assertEqual(response.execution.usage.input_tokens, 7)

    def test_antigravity_usage_remains_partial_and_version_sensitive(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "sessionId": "agy_session",
                "response": "Antigravity completed the task.",
                "usageMetadata": {
                    "inputTokens": 40,
                    "cachedInputTokens": 12,
                    "outputTokens": 8,
                    "totalTokens": 60,
                },
            }
        )
        adapter = self._adapter(
            AntigravityCliAdapter,
            StaticProcessRunner(process_result(stdout)),
            structured_output=True,
        )

        response = adapter.run(self.task, "run_antigravity")

        self.assertEqual(response.execution.provider_run_id, "agy_session")
        self.assertEqual(response.execution.usage.input_tokens, 40)
        self.assertEqual(response.execution.usage.cached_input_tokens, 12)
        self.assertEqual(response.execution.usage.completeness, "partial")
        self.assertTrue(
            any("version-sensitive" in item for item in response.execution.parse_warnings)
        )
        # --add-dir's value is a dynamic temp path, so check the stable flag pairs
        # rather than a rigid positional slice.
        command = response.execution.command
        self.assertEqual(command[1:5], ["--output-format", "stream-json", "--mode", "accept-edits"])
        self.assertIn("--add-dir", command)
        self.assertEqual(command[-2], "--prompt")

    def test_unavailable_cli_fails_closed_and_keeps_trace(self) -> None:
        result = process_result(
            "",
            return_code=None,
            unavailable=True,
            error="CLI executable not found: missing",
        )
        adapter = self._adapter(ClaudeCliAdapter, StaticProcessRunner(result))

        response = adapter.run(self.task, "run_missing")

        self.assertEqual(response.execution.status, "unavailable")
        self.assertEqual(response.execution.usage.completeness, "unavailable")
        self.assertIn("not found", response.execution.error)
        self.assertTrue(Path(response.execution.raw_trace_ref).exists())

    def test_nonzero_exit_is_failed_and_preserves_stderr(self) -> None:
        result = process_result("", return_code=7, stderr="provider command failed")
        adapter = self._adapter(ClaudeCliAdapter, StaticProcessRunner(result))

        response = adapter.run(self.task, "run_failed")

        self.assertEqual(response.execution.status, "failed")
        self.assertEqual(response.execution.exit_code, 7)
        self.assertEqual(response.execution.error, "provider command failed")
        self.assertTrue(Path(response.execution.stderr_ref).exists())

    def test_timeout_is_a_terminal_non_success_status(self) -> None:
        result = process_result(
            "",
            return_code=None,
            timed_out=True,
            error="CLI timed out after 1 seconds",
        )
        adapter = self._adapter(CodexCliAdapter, StaticProcessRunner(result))

        response = adapter.run(self.task, "run_timeout")

        self.assertEqual(response.execution.status, "timed_out")
        self.assertIsNone(response.execution.exit_code)
        self.assertEqual(response.execution.usage.completeness, "unavailable")

    def test_antigravity_text_mode_does_not_claim_usage(self) -> None:
        adapter = self._adapter(
            AntigravityCliAdapter,
            StaticProcessRunner(process_result("Antigravity text response")),
        )

        response = adapter.run(self.task, "run_antigravity_text")

        self.assertEqual(response.execution.usage.completeness, "unavailable")
        self.assertEqual(response.final_text, "Antigravity text response")
        command = response.execution.command
        self.assertEqual(command[1:3], ["--mode", "accept-edits"])
        self.assertIn("--add-dir", command)
        self.assertEqual(command[-2], "--prompt")
        self.assertNotIn("--output-format", response.execution.command)
        self.assertFalse(any("stdout line" in item for item in response.execution.parse_warnings))

    def test_role_profiles_pin_models_effort_permissions_and_minimal_controller(self) -> None:
        result = StaticProcessRunner(process_result(""))
        controller = self._adapter(
            CodexCliAdapter,
            result,
            role_profile_id="controller",
            model="gpt-5.6-luna",
            reasoning_effort="low",
            permission="read-only",
            context_policy="loop-snapshot",
            output_schema_path=output_schema_path("controller"),
        )
        controller.run(self.task, "run_controller")
        command = result.calls[0][0]
        self.assertIn("gpt-5.6-luna", command)
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn("read-only", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--output-schema", command)

        planner_runner = StaticProcessRunner(process_result(""))
        planner = self._adapter(
            ClaudeCliAdapter,
            planner_runner,
            role_profile_id="planner",
            model="sonnet",
            reasoning_effort="medium",
            permission="read-only",
            output_schema_path=output_schema_path("planner"),
        )
        planner.run(self.task, "run_planner")
        planner_command = planner_runner.calls[0][0]
        self.assertIn("sonnet", planner_command)
        self.assertIn("medium", planner_command)
        # Not "plan": that mode routes through Claude Code's interactive plan-mode UX
        # (ExitPlanMode / writing a ~/.claude/plans/*.md file), which breaks the "final
        # text is exactly one JSON object" contract when run headlessly (observed for
        # real on a Red Team run, 2026-08-12). "default" + disallowedTools gets the
        # same "cannot modify the repo" guarantee without that failure mode.
        self.assertEqual(
            planner_command[
                planner_command.index("--permission-mode") :
                planner_command.index("--permission-mode") + 2
            ],
            ["--permission-mode", "default"],
        )
        self.assertIn("--disallowedTools", planner_command)
        self.assertIn("Write,Edit,NotebookEdit", planner_command)
        self.assertIn("--json-schema", planner_command)

        developer_runner = StaticProcessRunner(process_result(""))
        developer = self._adapter(
            AntigravityCliAdapter,
            developer_runner,
            structured_output=True,
            role_profile_id="developer",
            model="gemini-3.1-pro-high",
            reasoning_effort="high",
            permission="workspace-write",
            output_schema_path=output_schema_path("developer"),
        )
        developer_response = developer.run(self.task, "run_developer")
        developer_command = developer_runner.calls[0][0]
        self.assertIn("gemini-3.1-pro-high", developer_command)
        # "gemini-3.1-pro-high" already bakes in the effort tier — passing a separate
        # --effort alongside it is a hard CLI error, so it must not be emitted here.
        self.assertNotIn("--effort", developer_command)
        self.assertIn("accept-edits", developer_command)
        self.assertIn("--json-schema", developer_command)
        self.assertIn("Inspect the repository", developer_command[-1])
        self.assertEqual(developer_response.execution.command[-1], "<task-prompt>")

    def _adapter(self, adapter_class, runner, **kwargs):
        return adapter_class(
            adapter_id=adapter_class.provider,
            role="Implementation Agent",
            executable="tool",
            working_directory=self.root,
            trace_root=self.root / "traces",
            timeout_seconds=42,
            process_runner=runner,
            **kwargs,
        )


class ReadOnlyWriteDetectionTests(TestCase):
    """A read-only role assigned to a CLI/mode that can't reliably be trusted to skip
    writes (e.g. Antigravity accept-edits used to dodge the --mode plan deadlock — see
    the antigravity-headless-command-permission and harness-agent-loop discussions)
    must not be able to silently modify the repo. cli.py's CliModelAdapter.run()
    compares git status before/after any permission="read-only" call and fails closed
    if anything changed — trace_root is deliberately kept OUTSIDE the git repo here,
    matching real config (config.trace_root lives in the Harness's own repo, never
    inside the target project), since a trace_root nested inside the working
    directory would itself look like a write and false-positive."""

    def setUp(self) -> None:
        import subprocess

        self.repo_dir = TemporaryDirectory()
        self.trace_dir = TemporaryDirectory()
        self.addCleanup(self.repo_dir.cleanup)
        self.addCleanup(self.trace_dir.cleanup)
        self.repo = Path(self.repo_dir.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.repo, check=True)
        self.task = TaskEnvelope(task_id="t1", workspace_id="ws_test", goal="audit only")

    def _adapter(self, runner, *, permission: str) -> ClaudeCliAdapter:
        return ClaudeCliAdapter(
            adapter_id="red-team",
            role="Red Team",
            executable="claude",
            working_directory=self.repo,
            trace_root=Path(self.trace_dir.name),
            timeout_seconds=30,
            process_runner=runner,
            permission=permission,
            role_profile_id="red-team",
        )

    def test_read_only_role_that_writes_fails_closed(self) -> None:
        class WritingRunner:
            def run(self, command, *, cwd, timeout_seconds, input_text=None):
                (Path(cwd) / "unexpected.txt").write_text("oops")
                return process_result('{"type":"result","result":"{}","usage":{}}')

        response = self._adapter(WritingRunner(), permission="read-only").run(self.task, "trace1/attempt0")
        self.assertEqual(response.execution.status, "failed")
        self.assertIn("safety violation", response.execution.error)
        self.assertIn("permission=read-only", response.execution.error)

    def test_read_only_role_that_does_not_write_passes(self) -> None:
        class CleanRunner:
            def run(self, command, *, cwd, timeout_seconds, input_text=None):
                return process_result('{"type":"result","result":"{}","usage":{}}')

        response = self._adapter(CleanRunner(), permission="read-only").run(self.task, "trace1/attempt1")
        self.assertEqual(response.execution.status, "succeeded")
        self.assertIsNone(response.execution.error)

    def test_workspace_write_role_writing_is_not_flagged(self) -> None:
        class WritingRunner:
            def run(self, command, *, cwd, timeout_seconds, input_text=None):
                (Path(cwd) / "expected.txt").write_text("this is fine, Developer can write")
                return process_result('{"type":"result","result":"{}","usage":{}}')

        response = self._adapter(WritingRunner(), permission="workspace-write").run(self.task, "trace1/attempt2")
        self.assertEqual(response.execution.status, "succeeded")
