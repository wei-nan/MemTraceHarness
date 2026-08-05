from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.adapters import ClaudeCliAdapter
from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.runner import HarnessRunner
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore


class StaticProcessRunner:
    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        input_text: str | None = None,
    ) -> ProcessResult:
        stdout = json.dumps(
            {
                "type": "result",
                "session_id": "session_test",
                "result": "Implementation evidence is ready for human review.",
                "usage": {"input_tokens": 5, "output_tokens": 2},
            }
        )
        return ProcessResult(
            command=command,
            return_code=0,
            stdout=stdout,
            stderr="",
            started_at="2026-08-03T00:00:00+00:00",
            completed_at="2026-08-03T00:00:01+00:00",
            duration_ms=1000,
        )


class RunnerTests(TestCase):
    def test_runner_persists_cli_usage_and_trace_reference(self) -> None:
        temp_dir_ctx = TemporaryDirectory()
        self.addCleanup(temp_dir_ctx.cleanup)
        root = Path(temp_dir_ctx.name)
        db_path = root / "trace.sqlite3"
        task = TaskEnvelope(
            task_id="task_test",
            workspace_id="ws_spec_plan",
            goal="Review harness boundary",
            context_refs=["mem_f2e46f6a"],
        )
        adapter = ClaudeCliAdapter(
            adapter_id="claude",
            role="Implementation Agent",
            executable="claude",
            working_directory=root,
            trace_root=root / "traces",
            timeout_seconds=60,
            process_runner=StaticProcessRunner(),
        )

        summary = HarnessRunner(adapters=[adapter], trace_store=TraceStore(db_path)).run(task)

        self.assertEqual(summary.run_mode, "cli_run")
        self.assertEqual(summary.responses[0].execution.status, "succeeded")
        self.assertIn("human review", summary.recommendation)
        with closing(sqlite3.connect(db_path)) as connection:
            execution = connection.execute(
                "SELECT provider, status, usage_json, raw_trace_ref FROM cli_executions"
            ).fetchone()
        self.assertEqual(execution[0:2], ("claude", "succeeded"))
        self.assertEqual(json.loads(execution[2])["input_tokens"], 5)
        self.assertTrue(Path(execution[3]).exists())
