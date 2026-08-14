from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import cast
from unittest import TestCase

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.loop import AgentLoopRunner
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schemas import (
    CliExecution,
    ModelResponse,
    TaskEnvelope,
    TokenUsage,
)
from memtrace_harness.trace_store import TraceStore


class QueueAdapter(ModelAdapter):
    def __init__(self, profile_id: str, provider: str, model: str, outputs: list[dict]) -> None:
        self.adapter_id = profile_id
        self.role = profile_id
        self.provider = provider
        self.model = model
        self.outputs = deque(outputs)
        self.calls: list[TaskEnvelope] = []
        self.quota_bucket = f"{provider}-account"

    def run(self, task: TaskEnvelope, trace_id: str) -> ModelResponse:
        self.calls.append(task)
        output = dict(self.outputs.popleft())
        completeness = output.pop("_usage", "complete")
        status = output.pop("_status", "succeeded")
        error = output.pop("_error", None)
        return ModelResponse(
            adapter_id=self.adapter_id,
            role=self.role,
            claims=[],
            final_text=json.dumps(output) if status == "succeeded" else "",
            requires_human_decision=True,
            execution=CliExecution(
                provider=self.provider,
                status=status,
                command=[self.provider],
                exit_code=0,
                started_at="2026-08-03T00:00:00+00:00",
                completed_at="2026-08-03T00:00:01+00:00",
                duration_ms=1000,
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=2,
                    completeness=completeness,
                ),
                role_profile_id=self.adapter_id,
                requested_model=self.model,
                reasoning_effort="high",
                permission=(
                    "workspace-write" if self.adapter_id == "developer" else "read-only"
                ),
                context_policy="test-policy",
                cli_version=f"{self.provider}-test-version",
                quota_bucket=self.quota_bucket,
                error=error,
            ),
        )


class FailingMemTraceClient:
    def create_node(self, **kwargs):
        raise RuntimeError("MemTrace unavailable")


def ready_plan(name: str = "plan") -> dict:
    return {
        "status": "ready",
        "plan": name,
        "acceptance_criteria": ["observable result"],
        "open_questions": [],
        "scope_exclusions": [],
    }


def passed_gate() -> dict:
    return {
        "verdict": "PASS",
        "reason_code": "none",
        "findings": [],
        "unverified_items": [],
        "confidence": 0.9,
    }


def completed_development() -> dict:
    return {
        "status": "completed",
        "summary": "implemented",
        "changed_files": ["src/example.py"],
        "tests": ["unit tests passed"],
        "gaps": [],
    }


class AgentLoopRunnerTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir_context = TemporaryDirectory()
        self.addCleanup(self.temp_dir_context.cleanup)
        self.root = Path(self.temp_dir_context.name)
        self.db_path = self.root / "trace.sqlite3"
        self.task = TaskEnvelope(
            task_id="task_loop",
            workspace_id="ws_spec_plan",
            goal="Implement the accepted change",
            risk_level="high",
        )

    def test_high_risk_happy_path_still_uses_sonnet_and_skips_opus(self) -> None:
        adapters = self._adapters()
        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(
            [stage.profile_id for stage in summary.stages],
            ["controller", "planner", "red-team", "developer", "red-team", "controller"],
        )
        self.assertEqual(len(adapters["planner"].calls), 1)
        self.assertEqual(len(adapters["planner-escalation"].calls), 0)
        self.assertEqual(summary.total_usage.input_tokens, 60)
        with closing(sqlite3.connect(self.db_path)) as connection:
            stage_count = connection.execute("SELECT COUNT(*) FROM loop_stages").fetchone()[0]
            planner_execution = connection.execute(
                "SELECT requested_model, cli_version, permission "
                "FROM cli_executions WHERE stage = 'plan'"
            ).fetchone()
        self.assertEqual(stage_count, 6)
        self.assertEqual(planner_execution, ("sonnet", "claude-test-version", "read-only"))

    def test_developer_needs_human_still_flows_to_g2_for_review(self) -> None:
        # A developer's own "needs_human"/"failed" status must not skip G2 and go
        # straight to a human — G2 is the reviewer built to catch exactly the kind of
        # gap a developer would self-report (missing compile evidence, expanded
        # scope), via the same REJECT verdict it already renders for a "completed"
        # development.
        adapters = self._adapters()
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [
                {
                    "status": "needs_human",
                    "summary": "blocked on tool permission denials, could not verify build",
                    "changed_files": ["src/example.py"],
                    "tests": ["written but not executed"],
                    "gaps": ["no compiler evidence available this session"],
                }
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(len(adapters["red-team"].calls), 2)
        self.assertEqual(summary.status, "succeeded")

    def test_developer_needs_human_g2_reject_triggers_one_revision(self) -> None:
        adapters = self._adapters()
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [
                {
                    "status": "needs_human",
                    "summary": "wrote the change but could not run tests",
                    "changed_files": ["src/example.py"],
                    "tests": ["written but not executed"],
                    "gaps": ["no compiler evidence available this session"],
                },
                completed_development(),
            ],
        )
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                passed_gate(),
                {
                    "verdict": "REJECT",
                    "reason_code": "test_gap",
                    "findings": [{"description": "no compiler/test evidence provided"}],
                    "unverified_items": [],
                    "confidence": 0.9,
                },
                passed_gate(),
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["developer"].calls), 2)
        self.assertIn("develop-revision", [stage.stage for stage in summary.stages])

    def test_resume_reuses_settled_stages_and_only_reruns_from_the_stop_point(
        self,
    ) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [
                {"action": "run_planner", "reason": "ready"},
                {"action": "run_planner", "reason": "still ready after clarification"},
                {"action": "finish", "reason": "complete"},
            ],
        )
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                {
                    "verdict": "NEEDS_HUMAN",
                    "reason_code": "acceptance_gap",
                    "findings": [{"description": "needs a human call on scope"}],
                    "unverified_items": [],
                    "confidence": 0.6,
                },
                passed_gate(),
                passed_gate(),
            ],
        )
        trace_store = TraceStore(self.db_path)

        first = AgentLoopRunner(adapters=adapters, trace_store=trace_store).run(
            self.task, conversation_id="chat_resume_test"
        )
        self.assertEqual(first.status, "needs_human")
        self.assertEqual(len(adapters["planner"].calls), 1)
        self.assertEqual(len(adapters["red-team"].calls), 1)

        second = AgentLoopRunner(adapters=adapters, trace_store=trace_store).run(
            self.task, conversation_id="chat_resume_test"
        )
        self.assertEqual(second.status, "succeeded")
        # The settled plan from the first run is reused, not regenerated.
        self.assertEqual(len(adapters["planner"].calls), 1)
        # G1 had not PASSed yet, so it (and everything after it) reruns.
        self.assertEqual(len(adapters["red-team"].calls), 3)
        self.assertEqual(len(adapters["developer"].calls), 1)

    def test_opus_is_used_only_after_g1_reasoning_gap(self) -> None:
        adapters = self._adapters()
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                {
                    "verdict": "REJECT",
                    "reason_code": "reasoning_gap",
                    "findings": [{"description": "missing tradeoff analysis"}],
                    "unverified_items": [],
                    "confidence": 0.9,
                },
                passed_gate(),
                passed_gate(),
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["planner-escalation"].calls), 1)
        self.assertIn("plan-escalation", [stage.stage for stage in summary.stages])
        self.assertIn("g1-recheck", [stage.stage for stage in summary.stages])

    def test_hard_budget_stops_when_usage_is_unavailable(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"action": "run_planner", "reason": "ready", "_usage": "unavailable"}],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
            max_total_tokens=100,
        ).run(self.task)

        self.assertEqual(summary.status, "budget_exhausted")
        self.assertEqual(len(summary.stages), 1)
        self.assertEqual(len(adapters["planner"].calls), 0)

    def test_non_reasoning_g1_reject_does_not_invoke_opus(self) -> None:
        adapters = self._adapters()
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                {
                    "verdict": "REJECT",
                    "reason_code": "missing_input",
                    "findings": [{"description": "product decision is missing"}],
                    "unverified_items": ["human choice"],
                    "confidence": 0.95,
                }
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "needs_human")
        self.assertEqual(len(adapters["planner-escalation"].calls), 0)
        self.assertEqual(len(adapters["developer"].calls), 0)

    def test_g1_acceptance_gap_gets_one_sonnet_revision_not_opus(self) -> None:
        adapters = self._adapters()
        adapters["planner"] = QueueAdapter(
            "planner",
            "claude",
            "sonnet",
            [ready_plan("first plan"), ready_plan("revised plan")],
        )
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                {
                    "verdict": "REJECT",
                    "reason_code": "acceptance_gap",
                    "findings": [{"description": "acceptance is not observable"}],
                    "unverified_items": [],
                    "confidence": 0.9,
                },
                passed_gate(),
                passed_gate(),
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["planner"].calls), 2)
        self.assertEqual(len(adapters["planner-escalation"].calls), 0)
        self.assertIn("plan-revision", [stage.stage for stage in summary.stages])

    def test_g2_reject_gets_one_gemini_revision_then_recheck(self) -> None:
        adapters = self._adapters()
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [completed_development(), completed_development()],
        )
        adapters["red-team"] = QueueAdapter(
            "red-team",
            "codex",
            "gpt-5.6-sol",
            [
                passed_gate(),
                {
                    "verdict": "REJECT",
                    "reason_code": "test_gap",
                    "findings": [{"description": "missing edge assertion"}],
                    "unverified_items": [],
                    "confidence": 0.9,
                },
                passed_gate(),
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["developer"].calls), 2)
        self.assertIn("develop-revision", [stage.stage for stage in summary.stages])
        self.assertIn("g2-recheck", [stage.stage for stage in summary.stages])

    def test_writeback_failure_keeps_local_loop_summary_and_executions(self) -> None:
        runner = AgentLoopRunner(
            adapters=self._adapters(),
            trace_store=TraceStore(self.db_path),
            memtrace_client=cast(MemTraceClient, FailingMemTraceClient()),
        )

        with self.assertRaisesRegex(RuntimeError, "MemTrace unavailable"):
            runner.run(self.task, writeback=True)

        with closing(sqlite3.connect(self.db_path)) as connection:
            run = connection.execute(
                "SELECT summary_json, writeback_node_id FROM runs"
            ).fetchone()
            execution_count = connection.execute(
                "SELECT COUNT(*) FROM cli_executions"
            ).fetchone()[0]
        self.assertIsNotNone(run[0])
        self.assertIsNone(run[1])
        self.assertEqual(execution_count, 6)

    def test_controller_quota_failure_falls_back_and_checkpoints_every_attempt(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"_status": "failed", "_error": "Quota exhausted for weekly limit"}],
        )
        adapters["red-team"].quota_bucket = "codex-red-team-test-account"
        fallback = QueueAdapter(
            "controller-fallback-1",
            "claude",
            "sonnet",
            [
                {"action": "run_planner", "reason": "continued from checkpoint"},
                {"action": "finish", "reason": "complete"},
            ],
        )

        task = replace(
            self.task,
            context_refs=["ws_harness/mem_root", "mem_local", "external-note"],
        )
        summary = AgentLoopRunner(
            adapters=adapters,
            fallback_adapters={"controller": [fallback]},
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
        ).run(task, conversation_id="conv_fallback")

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(summary.conversation_id, "conv_fallback")
        self.assertEqual(summary.stages[0].state, "failed")
        self.assertEqual(
            summary.stages[0].response.execution.failure_category,
            "quota_exhausted",
        )
        self.assertEqual(summary.stages[1].attempt_index, 1)
        self.assertEqual(summary.stages[1].response.execution.role_profile_id, "controller")
        self.assertEqual(summary.stages[1].response.execution.permission, "read-only")
        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(
            fallback.calls[0].context_items[-1].content_type,
            "harness_resume_envelope",
        )
        store = TraceStore(self.db_path)
        latest = store.latest_resume_envelope("conv_fallback")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.checkpoint_id, summary.active_checkpoint_id)
        with closing(sqlite3.connect(self.db_path)) as connection:
            attempts = connection.execute(
                "SELECT COUNT(*) FROM turns WHERE conversation_id = 'conv_fallback'"
            ).fetchone()[0]
            sessions = connection.execute(
                "SELECT COUNT(*) FROM provider_sessions "
                "WHERE conversation_id = 'conv_fallback'"
            ).fetchone()[0]
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM checkpoints "
                "WHERE conversation_id = 'conv_fallback'"
            ).fetchone()[0]
            failures = connection.execute(
                "SELECT failure_category FROM cli_executions "
                "WHERE run_id = ? ORDER BY sequence LIMIT 1",
                (summary.trace_id,),
            ).fetchone()[0]
            memory_refs = connection.execute(
                "SELECT workspace_id, node_id, ref FROM memory_refs "
                "WHERE conversation_id = 'conv_fallback' "
                "AND purpose = 'checkpoint-source' ORDER BY ref"
            ).fetchall()
        self.assertEqual(attempts, len(summary.stages))
        self.assertEqual(sessions, len(summary.stages))
        self.assertGreaterEqual(checkpoints, len(summary.stages))
        self.assertEqual(failures, "quota_exhausted")
        self.assertEqual(
            memory_refs,
            [
                (None, None, "external-note"),
                ("ws_spec_plan", "mem_local", "mem_local"),
                ("ws_harness", "mem_root", "ws_harness/mem_root"),
            ],
        )

    def test_shared_quota_bucket_stops_red_team_without_invoking_it(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"_status": "failed", "_error": "Quota exhausted for weekly limit"}],
        )
        fallback = QueueAdapter(
            "controller-fallback-1",
            "claude",
            "sonnet",
            [{"action": "run_planner", "reason": "continued"}],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            fallback_adapters={"controller": [fallback]},
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "failed")
        self.assertEqual(len(adapters["red-team"].calls), 0)
        self.assertEqual(summary.stages[-1].state, "quota_cooldown")
        self.assertEqual(summary.stages[-1].profile_id, "red-team")

    def test_authentication_failure_does_not_use_fallback(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"_status": "failed", "_error": "login required"}],
        )
        fallback = QueueAdapter(
            "controller-fallback-1",
            "claude",
            "sonnet",
            [{"action": "run_planner", "reason": "should not run"}],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            fallback_adapters={"controller": [fallback]},
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "failed")
        self.assertEqual(len(fallback.calls), 0)
        self.assertEqual(
            summary.stages[0].response.execution.failure_category,
            "authentication",
        )

    def test_existing_conversation_injects_latest_resume_envelope(self) -> None:
        store = TraceStore(self.db_path)
        first = AgentLoopRunner(
            adapters=self._adapters(),
            trace_store=store,
        ).run(self.task, conversation_id="conv_resume")
        second_adapters = self._adapters()

        second = AgentLoopRunner(
            adapters=second_adapters,
            trace_store=store,
        ).run(
            replace(self.task, task_id="task_new_cli_invocation"),
            conversation_id="conv_resume",
        )

        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(second.task.task_id, self.task.task_id)
        controller_context = second_adapters["controller"].calls[0].context_items
        self.assertTrue(
            any(item.content_type == "harness_resume_envelope" for item in controller_context)
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE conversation_id = 'conv_resume'"
            ).fetchone()[0]
        self.assertEqual(run_count, 2)

    def _adapters(self) -> dict[str, QueueAdapter]:
        return {
            "controller": QueueAdapter(
                "controller",
                "codex",
                "gpt-5.6-luna",
                [
                    {"action": "run_planner", "reason": "ready"},
                    {"action": "finish", "reason": "complete"},
                ],
            ),
            "planner": QueueAdapter("planner", "claude", "sonnet", [ready_plan()]),
            "planner-escalation": QueueAdapter(
                "planner-escalation", "claude", "opus", [ready_plan("deep plan")]
            ),
            "red-team": QueueAdapter(
                "red-team", "codex", "gpt-5.6-sol", [passed_gate(), passed_gate()]
            ),
            "developer": QueueAdapter(
                "developer",
                "antigravity",
                "gemini-3.1-pro-high",
                [completed_development()],
            ),
        }
