from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.adapters import MockModelAdapter
from memtrace_harness.runner import HarnessRunner
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore



class RunnerTests(TestCase):
    def test_runner_creates_draft_first_summary(self) -> None:
        temp_dir_ctx = TemporaryDirectory()
        self.addCleanup(temp_dir_ctx.cleanup)
        temp_dir = Path(temp_dir_ctx.name)

        task = TaskEnvelope(
            task_id="task_test",
            workspace_id="ws_spec_plan",
            goal="Review harness boundary",
            context_refs=["mem_f178ea8d"],
        )
        runner = HarnessRunner(
            adapters=[
                MockModelAdapter("mock-planner", "Planner"),
                MockModelAdapter("mock-reviewer", "Evidence Judge"),
            ],
            trace_store=TraceStore(temp_dir / "trace.sqlite3"),
        )

        summary = runner.run(task)

        self.assertEqual(summary.run_mode, "dry_run")
        self.assertTrue(summary.trace_id.startswith("run_"))
        self.assertEqual(summary.responses[0].claims[0].evidence_refs, ["mem_f178ea8d"])
        self.assertIn("human review", summary.recommendation)
