from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.loop import (
    AgentLoopRunner,
    _is_technical_output_failure,
    _plan_structure_ok,
    controller_task,
    developer_revision_task,
    developer_task,
    gate_task,
    operational_task,
    planner_escalation_task,
    planner_revision_task,
    planner_task,
    summarize_invalid_artifact,
    summarize_plan_needs_human,
    valid_plan,
)
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schemas import (
    CliExecution,
    ContextItem,
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
        raw_text = output.pop("_raw_text", None)
        return ModelResponse(
            adapter_id=self.adapter_id,
            role=self.role,
            claims=[],
            final_text=(
                raw_text if raw_text is not None
                else json.dumps(output) if status == "succeeded" else ""
            ),
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


class TechnicalOutputFailureDetectionTests(TestCase):
    def test_schema_validation_failure_is_technical(self) -> None:
        text = summarize_invalid_artifact("Controller", {"action": "finish", "reason": "..."})
        self.assertTrue(_is_technical_output_failure(text))

    def test_missing_structured_output_is_technical(self) -> None:
        self.assertTrue(_is_technical_output_failure(summarize_invalid_artifact("Planner", None)))

    def test_raw_invalid_output_fallback_text_is_technical(self) -> None:
        self.assertTrue(
            _is_technical_output_failure(
                "planner returned invalid structured output; no stage advanced.\nModel said:\n..."
            )
        )

    def test_genuine_open_question_is_not_technical(self) -> None:
        # A real G1/Controller "ask_human" recommendation must NOT be misrouted —
        # only a parsing/schema failure counts.
        self.assertFalse(
            _is_technical_output_failure(
                "G1 verdict: NEEDS_HUMAN (reasoning_gap)\n- [?] scope 邊界未確認"
            )
        )


class WellFormedNeedsHumanPlanTests(TestCase):
    """2026-09-05 bug (run_3fd1adaf7e8a): valid_plan() rejected ANY status other
    than "ready", including a complete, well-formed plan that honestly reported
    status="needs_human" with real open_questions — that got misrouted through
    summarize_invalid_artifact() as "structured output was returned but failed
    schema validation", sounding like a retry-fixable parsing glitch when the
    model had done its job correctly and a human genuinely needed to answer
    something. Fixed by splitting the structural check (_plan_structure_ok) from
    the status=="ready" gate (valid_plan)."""

    def _well_formed_needs_human_plan(self) -> dict:
        return {
            "status": "needs_human",
            "plan": "A complete, valid plan that honestly needs human input.",
            "acceptance_criteria": ["criterion one"],
            "open_questions": ["What should happen to the orphaned service?"],
            "scope_exclusions": ["Not touching unrelated feature X."],
        }

    def test_well_formed_needs_human_plan_is_not_a_valid_ready_plan(self) -> None:
        # valid_plan() still means "ready to hand to Dev" — a needs_human plan,
        # however well-formed, is correctly NOT that.
        self.assertFalse(valid_plan(self._well_formed_needs_human_plan()))

    def test_well_formed_needs_human_plan_passes_structure_check(self) -> None:
        self.assertTrue(_plan_structure_ok(self._well_formed_needs_human_plan()))

    def test_genuinely_malformed_plan_fails_structure_check(self) -> None:
        self.assertFalse(_plan_structure_ok({"status": "needs_human", "plan": ""}))
        self.assertFalse(_plan_structure_ok(None))

    def test_well_formed_needs_human_plan_is_not_classified_as_technical_failure(self) -> None:
        detail = summarize_plan_needs_human("Planner", self._well_formed_needs_human_plan())
        self.assertIn("What should happen to the orphaned service?", detail)
        self.assertIn("A complete, valid plan", detail)
        self.assertFalse(_is_technical_output_failure(detail))


class ControllerTaskContextTests(TestCase):
    def test_controller_sees_project_scope_and_prior_discussion(self) -> None:
        # 2026-09-05: Controller used to be filtered down to ONLY loop-snapshot +
        # harness_resume_envelope items, silently dropping the project-scope and
        # prior-discussion context TelegramGateway attaches to every chat-triggered
        # task — which is exactly why it kept saying "I don't have context" for
        # goals like "接續 chat_3077a492 的任務" (chat_621c8bb9, generation 17).
        base_task = TaskEnvelope(
            task_id="task_1",
            workspace_id="ws_test",
            goal="接續 chat_3077a492 的任務",
            context_refs=[],
            context_items=[
                ContextItem(
                    ref="harness:project-scope:Beri",
                    title="Project scope",
                    body="# Beri scope",
                    content_type="context",
                    source="harness",
                ),
                ContextItem(
                    ref="harness:knowledge-bases:Beri",
                    title="Knowledge-base locations",
                    body="Cold memory: `ws_memory`",
                    content_type="context",
                    source="harness",
                ),
                ContextItem(
                    ref="harness:prior-discussion:Beri",
                    title="Prior discussion",
                    body="Earlier substantive context: ...",
                    content_type="context",
                    source="harness",
                ),
                ContextItem(
                    ref="harness:resume:conv_1",
                    title="Resume envelope",
                    body="{}",
                    content_type="harness_resume_envelope",
                    source="harness",
                ),
                ContextItem(
                    ref="harness:accepted-plan",
                    title="Accepted plan",
                    body="{}",
                    content_type="artifact",
                    source="harness",
                ),
            ],
        )
        result = controller_task(base_task, stage="start", stages=[])
        refs = {item.ref for item in result.context_items}
        self.assertIn("harness:project-scope:Beri", refs)
        self.assertIn("harness:knowledge-bases:Beri", refs)
        self.assertIn("harness:prior-discussion:Beri", refs)
        self.assertIn("harness:resume:conv_1", refs)
        # Still excludes unrelated artifact-type items — Controller's sandbox isn't
        # thrown wide open, just given the same static text Planner already sees.
        self.assertNotIn("harness:accepted-plan", refs)

    def test_controller_is_allowed_to_search_memtrace_but_not_other_tools(self) -> None:
        # 2026-09-05: the user explicitly asked that Controller be able to search
        # MemTrace/history itself (deciding when it's actually needed) rather than
        # being fully tool-blind, while staying honest about whether it searched.
        task = TaskEnvelope(
            task_id="task_1", workspace_id="ws_test", goal="接續 chat_3077a492 的任務"
        )
        result = controller_task(task, stage="start", stages=[])
        goal_text = result.goal
        self.assertIn("search_nodes", goal_text)
        self.assertIn("searched_history", goal_text)
        self.assertIn("no git command", goal_text)

    def test_only_converge_stage_may_propose_kb_updates_and_none_calls_a_write_tool(self) -> None:
        task = TaskEnvelope(
            task_id="task_1", workspace_id="ws_test", goal="do the thing"
        )
        start_goal = controller_task(task, stage="start", stages=[]).goal
        converge_goal = controller_task(task, stage="converge", stages=[]).goal
        self.assertIn("kb_updates must be null", start_goal)
        self.assertIn("claim_done", converge_goal)
        self.assertIn("You do not call any write tool", converge_goal)
        self.assertNotIn("claim_done", start_goal)

    def test_operational_runs_are_told_to_use_the_taiwantrade_tools_instead_of_curl(self) -> None:
        from unittest.mock import patch

        task = TaskEnvelope(task_id="task_1", workspace_id="ws_test", goal="check positions")
        spec = {"command": "python", "args": ["-m", "x"], "env": {}}
        with patch("memtrace_harness.loop.mcp_server_spec", return_value=spec):
            goal = operational_task(task).goal
        self.assertIn("get_positions", goal)
        self.assertIn("instead of requesting http://127.0.0.1:8000", goal)
        self.assertIn("answered 401", goal)
        with patch("memtrace_harness.loop.mcp_server_spec", return_value=None):
            self.assertNotIn("get_positions", operational_task(task).goal)

    def test_planner_and_developer_check_the_tool_catalog_and_stop_when_it_has_a_gap(self) -> None:
        from unittest.mock import patch

        task = TaskEnvelope(task_id="task_1", workspace_id="ws_test", goal="add a TAIEX chart fetch")
        env = {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/k"}
        with patch.dict("os.environ", env):
            for goal in (
                planner_task(task).goal,
                planner_revision_task(task, {}, {}).goal,
                planner_escalation_task(task, {}, {}).goal,
                developer_task(task, {}).goal,
                developer_revision_task(task, {}, {}, {}).goal,
            ):
                self.assertIn("get_positions", goal)
                self.assertIn("not available to Agent Loop roles".lower(), goal.lower())
                self.assertIn("needs_human", goal)
                self.assertIn("do not build your own client", goal)
        with patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("get_positions", planner_task(task).goal)

    def test_start_stage_explains_run_operational_action_choice(self) -> None:
        task = TaskEnvelope(task_id="task_1", workspace_id="ws_test", goal="check something")
        goal_text = controller_task(task, stage="start", stages=[]).goal
        self.assertIn("run_operational_action", goal_text)
        self.assertIn("run_planner", goal_text)
        self.assertIn(
            "['run_planner', 'run_operational_action', 'ask_human', 'stop']", goal_text
        )

    def test_agent_loop_disabled_strips_run_planner_from_allowed_actions(self) -> None:
        task = TaskEnvelope(task_id="task_1", workspace_id="ws_test", goal="check something")
        goal_text = controller_task(
            task, stage="start", stages=[], agent_loop_enabled=False
        ).goal
        self.assertIn(
            "['run_operational_action', 'ask_human', 'stop']", goal_text
        )
        self.assertNotIn(
            "['run_planner', 'run_operational_action', 'ask_human', 'stop']", goal_text
        )
        self.assertIn("agent_loop is disabled", goal_text)

    def test_converge_stage_ignores_agent_loop_enabled(self) -> None:
        # agent_loop_enabled only changes what's valid at the "start" decision —
        # converge's own allowed set (finish/ask_human) never mentions run_planner
        # in the first place, so passing False here must not change its text at all.
        task = TaskEnvelope(task_id="task_1", workspace_id="ws_test", goal="check something")
        enabled_goal = controller_task(
            task, stage="converge", stages=[], agent_loop_enabled=True
        ).goal
        disabled_goal = controller_task(
            task, stage="converge", stages=[], agent_loop_enabled=False
        ).goal
        self.assertEqual(enabled_goal, disabled_goal)


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

    def test_run_operational_action_skips_planner_and_review(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"action": "run_operational_action", "reason": "just needs a check"}],
        )
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [
                {
                    "status": "completed",
                    "summary": "2891 is up 3%, well above the 5% stop-loss",
                    "changed_files": [],
                    "tests": [],
                    "gaps": [],
                }
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertIn("5% stop-loss", summary.recommendation)
        self.assertEqual(
            [stage.profile_id for stage in summary.stages], ["controller", "developer"]
        )
        self.assertEqual(len(adapters["planner"].calls), 0)
        self.assertEqual(len(adapters["red-team"].calls), 0)
        self.assertEqual(len(adapters["developer"].calls), 1)
        self.assertIn(
            "NOT a code-development task", adapters["developer"].calls[0].goal
        )

    def test_run_operational_action_needs_human_is_reported_without_g2(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"action": "run_operational_action", "reason": "just needs a check"}],
        )
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [
                {
                    "status": "needs_human",
                    "summary": "no quote source configured, cannot fetch a live price",
                    "changed_files": [],
                    "tests": [],
                    "gaps": ["missing quote source config"],
                }
            ],
        )

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("quote source", summary.recommendation)
        self.assertEqual(len(adapters["red-team"].calls), 0)

    def test_agent_loop_disabled_blocks_run_planner_deterministically(self) -> None:
        # A model picking run_planner anyway despite controller_task()'s own
        # instruction (agent_loop_enabled=False strips it from the allowed list) must
        # still be blocked structurally — never reach Planner/Developer.
        adapters = self._adapters()  # controller queue defaults to run_planner first

        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
            agent_loop_enabled=False,
        ).run(self.task)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("agent_loop", summary.recommendation)
        self.assertIn("disabled", summary.recommendation)
        self.assertEqual(len(adapters["planner"].calls), 0)
        self.assertEqual(len(adapters["developer"].calls), 0)

    def test_agent_loop_enabled_by_default_allows_run_planner(self) -> None:
        # Sanity check that the new constructor default doesn't change any existing
        # behavior for a runner that never opts into agent_loop_enabled=False.
        adapters = self._adapters()
        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
        ).run(self.task)
        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["planner"].calls), 1)

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

    def test_verify_command_failure_skips_g2_model_and_forces_a_revision(self) -> None:
        # A failing deterministic build/test command must be a hard precondition —
        # G2's model is never even asked, and the resulting synthetic REJECT drives
        # the same one-bounded-revision loop a real G2 REJECT would.
        adapters = self._adapters()
        adapters["developer"] = QueueAdapter(
            "developer",
            "antigravity",
            "gemini-3.1-pro-high",
            [completed_development(), completed_development()],
        )
        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
            working_directory=self.root,
            verify_command="exit 1",
        ).run(self.task)

        self.assertEqual(summary.status, "needs_human")
        # Only G1 called the red-team model; both G2 attempts were pre-empted by the
        # failing verification command instead of reaching the model.
        self.assertEqual(len(adapters["red-team"].calls), 1)
        self.assertEqual(len(adapters["developer"].calls), 2)
        stage_names = [stage.stage for stage in summary.stages]
        self.assertIn("g2", stage_names)
        self.assertIn("develop-revision", stage_names)
        self.assertIn("g2-recheck", stage_names)

    def test_verify_command_success_still_lets_g2_model_review(self) -> None:
        adapters = self._adapters()
        summary = AgentLoopRunner(
            adapters=adapters,
            trace_store=TraceStore(self.db_path),
            working_directory=self.root,
            verify_command="exit 0",
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(len(adapters["red-team"].calls), 2)

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

    def _g1_implementation_gap(self) -> dict:
        return {
            "verdict": "REJECT",
            "reason_code": "implementation_gap",
            "findings": [{"description": "the feature does not exist in the repository yet"}],
            "unverified_items": [],
            "confidence": 0.98,
        }

    def test_g1_judges_the_plan_not_the_missing_implementation(self) -> None:
        goal_g1 = gate_task(self.task, "G1", ready_plan()).goal
        goal_g2 = gate_task(self.task, "G2", completed_development(), ready_plan()).goal
        self.assertIn("PLAN gate", goal_g1)
        self.assertIn("NEVER a finding", goal_g1)
        self.assertNotIn("PLAN gate", goal_g2)
        self.assertIn("IMPLEMENTATION gate", goal_g2)
        self.assertIn("CORRECTION", gate_task(self.task, "G1", ready_plan(), rescope=True).goal)
        self.assertNotIn("CORRECTION", goal_g1)

    def test_both_gates_pass_by_default_and_never_require_a_git_diff(self) -> None:
        for goal in (
            gate_task(self.task, "G1", ready_plan()).goal,
            gate_task(self.task, "G2", completed_development(), ready_plan()).goal,
        ):
            self.assertIn("PASS BY DEFAULT", goal)
            self.assertIn("safety or security violation", goal)
            self.assertIn("Do NOT require a git repository, a git diff", goal)

    def test_g1_implementation_gap_is_asked_again_not_sent_to_plan_revision(self) -> None:
        adapters = self._adapters()
        adapters["red-team"] = QueueAdapter(
            "red-team", "codex", "gpt-5.6-sol",
            [self._g1_implementation_gap(), passed_gate(), passed_gate()],
        )

        summary = AgentLoopRunner(
            adapters=adapters, trace_store=TraceStore(self.db_path)
        ).run(self.task)

        self.assertEqual(summary.status, "succeeded")
        stages = [stage.stage for stage in summary.stages]
        self.assertIn("g1-rescope", stages)
        self.assertNotIn("plan-revision", stages)
        self.assertEqual(len(adapters["planner"].calls), 1)          # no revision round
        self.assertIn("CORRECTION", adapters["red-team"].calls[1].goal)

    def test_g1_implementation_gap_twice_stops_without_a_pointless_revision(self) -> None:
        adapters = self._adapters()
        adapters["red-team"] = QueueAdapter(
            "red-team", "codex", "gpt-5.6-sol",
            [self._g1_implementation_gap(), self._g1_implementation_gap()],
        )

        summary = AgentLoopRunner(
            adapters=adapters, trace_store=TraceStore(self.db_path)
        ).run(self.task)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("cannot apply before any code exists", summary.recommendation)
        self.assertEqual(len(adapters["planner"].calls), 1)
        self.assertEqual(len(adapters["developer"].calls), 0)
        self.assertNotIn("plan-revision", [stage.stage for stage in summary.stages])

    def test_g1_test_gap_is_treated_the_same_way(self) -> None:
        adapters = self._adapters()
        gap = {**self._g1_implementation_gap(), "reason_code": "test_gap"}
        adapters["red-team"] = QueueAdapter(
            "red-team", "codex", "gpt-5.6-sol", [gap, passed_gate(), passed_gate()]
        )
        summary = AgentLoopRunner(
            adapters=adapters, trace_store=TraceStore(self.db_path)
        ).run(self.task)
        self.assertEqual(summary.status, "succeeded")
        self.assertIn("g1-rescope", [stage.stage for stage in summary.stages])

    def test_a_genuine_plan_defect_still_gets_its_revision(self) -> None:
        # acceptance_gap is a real plan defect: unchanged behavior, no rescope round.
        adapters = self._adapters()
        adapters["planner"] = QueueAdapter(
            "planner", "claude", "sonnet", [ready_plan("first"), ready_plan("revised")]
        )
        adapters["red-team"] = QueueAdapter(
            "red-team", "codex", "gpt-5.6-sol",
            [
                {**self._g1_implementation_gap(), "reason_code": "acceptance_gap"},
                passed_gate(),
                passed_gate(),
            ],
        )
        summary = AgentLoopRunner(
            adapters=adapters, trace_store=TraceStore(self.db_path)
        ).run(self.task)
        stages = [stage.stage for stage in summary.stages]
        self.assertIn("plan-revision", stages)
        self.assertNotIn("g1-rescope", stages)

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

    def test_agent_loop_disabled_stop_is_routed_to_config_change_required(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"action": "ask_human", "reason": "需要改程式碼，但專案 agent_loop 已停用"}],
        )
        requested: list[str] = []

        class FakeApprovals:
            def request_approval(self, **kwargs):
                requested.append(kwargs["reason"])

        AgentLoopRunner(
            adapters=adapters,
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
            approval_manager=FakeApprovals(),
            agent_loop_enabled=False,
        ).run(self.task)

        self.assertEqual(requested, ["config_change_required"])

    def _needs_human_plan(self, **extra) -> dict:
        return {
            "status": "needs_human",
            "plan": "blocked on a product decision",
            "acceptance_criteria": [],
            "open_questions": ["migrate old rows?"],
            "scope_exclusions": [],
            **extra,
        }

    def _run_stopped_planner(self, plan: dict) -> list[dict]:
        adapters = self._adapters()
        adapters["planner"] = QueueAdapter("planner", "claude", "sonnet", [plan])
        requests: list[dict] = []

        class FakeApprovals:
            def request_approval(self, **kwargs):
                requests.append(kwargs)

        summary = AgentLoopRunner(
            adapters=adapters,
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
            approval_manager=FakeApprovals(),
        ).run(self.task)
        self.assertEqual(summary.status, "needs_human")
        return requests

    def test_stopped_stage_decision_card_reaches_the_approval_request(self) -> None:
        card = {
            "situation": "舊資料要不要遷移沒有說明。",
            "options": [
                {"label": "不遷移", "action": "只對新資料生效", "tradeoff": "舊資料格式不同"},
                {"label": "遷移", "action": "寫遷移腳本", "tradeoff": "需要備份"},
            ],
            "recommended": 0,
            "reason": "沒有讀取端依賴。",
            "default_if_silent": "維持暫停。",
        }
        requests = self._run_stopped_planner(self._needs_human_plan(decision_card=card))

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["decision_card"], {**card, "basis": []})
        self.assertIn("migrate old rows?", requests[0]["proposed_action"])

    def test_stop_without_a_usable_card_falls_back_to_the_old_request(self) -> None:
        for extra in ({}, {"decision_card": None}, {"decision_card": {"situation": "只有一半"}}):
            with self.subTest(extra=extra):
                requests = self._run_stopped_planner(self._needs_human_plan(**extra))
                self.assertEqual(len(requests), 1)
                self.assertIsNone(requests[0]["decision_card"])

    def _precedent_task(self) -> TaskEnvelope:
        return TaskEnvelope(
            task_id="task_precedent",
            workspace_id="ws_spec_plan",
            goal="Implement the accepted change",
            risk_level="high",
            context_items=[
                ContextItem(
                    ref="harness:project-scope:p", title="Project scope", body="scope",
                    content_type="context", source="harness",
                ),
                ContextItem(
                    ref="harness:operator-precedent:p", title="Operator precedent",
                    body="- D7 · 2026-10-05 · unattended_write · x → B\n[#3] 簡短回報",
                    content_type="operator_precedent", source="harness",
                ),
            ],
        )

    def test_only_the_controller_is_shown_the_operator_precedent(self) -> None:
        adapters = self._adapters()
        AgentLoopRunner(adapters=adapters, trace_store=TraceStore(self.db_path)).run(
            self._precedent_task()
        )

        def refs(role: str) -> set[str]:
            return {i.ref for call in adapters[role].calls for i in call.context_items}

        self.assertIn("harness:operator-precedent:p", refs("controller"))
        self.assertNotIn("harness:operator-precedent:p", refs("planner"))
        self.assertNotIn("harness:operator-precedent:p", refs("developer"))
        self.assertNotIn("harness:operator-precedent:p", refs("red-team"))

    def test_a_card_can_only_cite_precedents_it_was_shown(self) -> None:
        card = {
            "situation": "s",
            "options": [
                {"label": "a", "action": "a", "tradeoff": "a"},
                {"label": "b", "action": "b", "tradeoff": "b"},
            ],
            "recommended": 0,
            "reason": "r",
            "default_if_silent": "d",
            "basis": ["D7", "P3", "D99", "P1"],
        }
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller", "codex", "gpt-5.6-luna",
            [{"action": "ask_human", "reason": "unclear", "decision_card": card}],
        )
        requests: list[dict] = []

        class FakeApprovals:
            def request_approval(self, **kwargs):
                requests.append(kwargs)

        AgentLoopRunner(
            adapters=adapters, role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path), approval_manager=FakeApprovals(),
        ).run(self._precedent_task())

        self.assertEqual(requests[0]["decision_card"]["basis"], ["D7", "P3"])

    def test_unlaunchable_cli_falls_back_and_alerts_operator(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            # The primary is re-tried on every controller stage (no cooldown for a launch
            # failure), so it fails once per stage and falls back each time.
            [{"_status": "failed", "_error": "exec: /x/codex: cannot execute: No such file or directory"}] * 2,
        )
        fallback = QueueAdapter(
            "controller-fallback-1",
            "claude",
            "sonnet",
            [
                {"action": "run_planner", "reason": "continued"},
                {"action": "finish", "reason": "complete"},
            ],
        )
        alerts: list[str] = []

        AgentLoopRunner(
            adapters=adapters,
            fallback_adapters={"controller": [fallback]},
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
            alert_callback=alerts.append,
        ).run(self.task)

        self.assertEqual(len(fallback.calls), 2)
        self.assertEqual(len(alerts), 2)
        self.assertIn("codex/gpt-5.6-luna", alerts[0])
        self.assertIn("claude/sonnet", alerts[0])

    def test_unlaunchable_cli_without_fallback_still_alerts(self) -> None:
        adapters = self._adapters()
        adapters["controller"] = QueueAdapter(
            "controller",
            "codex",
            "gpt-5.6-luna",
            [{"_status": "failed", "_error": "CLI executable not found: codex"}],
        )
        alerts: list[str] = []

        summary = AgentLoopRunner(
            adapters=adapters,
            role_profiles=load_role_profiles(),
            trace_store=TraceStore(self.db_path),
            alert_callback=alerts.append,
        ).run(self.task)

        self.assertEqual(summary.status, "failed")
        self.assertEqual(len(alerts), 1)
        self.assertIn("沒有可用的備援", alerts[0])

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


def _stub_adapters() -> dict[str, QueueAdapter]:
    """Satisfies AgentLoopRunner._validate_adapters()'s required-role check for
    tests that only need a runner instance to call a helper method directly,
    never .run() — the queues are empty because they're never dequeued."""
    return {
        role: QueueAdapter(role, "codex", "gpt-5.6-luna", [])
        for role in ("controller", "planner", "planner-escalation", "red-team", "developer")
    }


class _FakeChatConfig:
    def __init__(self, provider: str | None = "claude", model: str | None = "haiku") -> None:
        self.chat_provider = provider
        self.chat_model = model

    def command_for(self, provider: str) -> str:
        return provider

    # The real argv builder, against this fake's command_for.
    from memtrace_harness.config import HarnessConfig as _HarnessConfig

    chat_command = _HarnessConfig.chat_command


class MalformedOutputRepairTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir_context = TemporaryDirectory()
        self.addCleanup(self.temp_dir_context.cleanup)
        self.root = Path(self.temp_dir_context.name)
        self.db_path = self.root / "trace.sqlite3"

    def test_no_config_returns_none_without_any_cli_call(self) -> None:
        runner = AgentLoopRunner(adapters=_stub_adapters(), trace_store=TraceStore(self.db_path))
        self.assertIsNone(
            runner._repair_malformed_output(profile_id="controller", raw_text="I did the thing.")
        )

    def test_repair_reformats_prose_into_the_stage_schema(self) -> None:
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        runner = AgentLoopRunner(
            adapters=_stub_adapters(),
            trace_store=TraceStore(self.db_path),
            config=cast(Any, _FakeChatConfig()),
        )
        repaired_json = json.dumps({"action": "run_planner", "reason": "ready to proceed"})
        with patch(
            "memtrace_harness.cli_process.CliProcessRunner.run",
            return_value=ProcessResult(
                command=["claude"],
                return_code=0,
                stdout=repaired_json,
                stderr="",
                started_at="2026-09-05T00:00:00+00:00",
                completed_at="2026-09-05T00:00:01+00:00",
                duration_ms=100,
            ),
        ):
            result = runner._repair_malformed_output(
                profile_id="controller",
                raw_text="Sure! I've decided we should run the planner next because it's ready.",
            )
        self.assertEqual(result, {"action": "run_planner", "reason": "ready to proceed"})

    def test_repair_returning_unparseable_text_yields_none(self) -> None:
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        runner = AgentLoopRunner(
            adapters=_stub_adapters(),
            trace_store=TraceStore(self.db_path),
            config=cast(Any, _FakeChatConfig()),
        )
        with patch(
            "memtrace_harness.cli_process.CliProcessRunner.run",
            return_value=ProcessResult(
                command=["claude"],
                return_code=0,
                stdout="I'm not sure how to express that as JSON.",
                stderr="",
                started_at="2026-09-05T00:00:00+00:00",
                completed_at="2026-09-05T00:00:01+00:00",
                duration_ms=100,
            ),
        ):
            result = runner._repair_malformed_output(profile_id="controller", raw_text="garbled")
        self.assertIsNone(result)

    def test_execute_recovers_a_prose_wrapped_controller_response(self) -> None:
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        task = TaskEnvelope(
            task_id="task_repair",
            workspace_id="ws_spec_plan",
            goal="Implement the accepted change",
            risk_level="high",
        )
        adapters: dict[str, QueueAdapter] = {
            "controller": QueueAdapter(
                "controller",
                "codex",
                "gpt-5.6-luna",
                [
                    {
                        "_raw_text": (
                            "I've decided to run the planner next since the request is ready. "
                            'Here is my JSON-ish answer: {action: run_planner, reason: ready}'
                        )
                    },
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
        repaired_json = json.dumps({"action": "run_planner", "reason": "ready to proceed"})
        with patch(
            "memtrace_harness.cli_process.CliProcessRunner.run",
            return_value=ProcessResult(
                command=["claude"],
                return_code=0,
                stdout=repaired_json,
                stderr="",
                started_at="2026-09-05T00:00:00+00:00",
                completed_at="2026-09-05T00:00:01+00:00",
                duration_ms=100,
            ),
        ):
            summary = AgentLoopRunner(
                adapters=adapters,
                trace_store=TraceStore(self.db_path),
                config=cast(Any, _FakeChatConfig()),
            ).run(task)

        self.assertEqual(summary.status, "succeeded")
        controller_stage = summary.stages[0]
        self.assertEqual(controller_stage.state, "succeeded")
        self.assertEqual(controller_stage.artifact, {"action": "run_planner", "reason": "ready to proceed"})
        self.assertIn(
            "final_text was not valid JSON; repaired via chat-model reformatting",
            controller_stage.response.execution.parse_warnings,
        )

    def test_execute_falls_back_to_invalid_output_when_repair_also_fails(self) -> None:
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        task = TaskEnvelope(
            task_id="task_repair_fail",
            workspace_id="ws_spec_plan",
            goal="Implement the accepted change",
            risk_level="high",
        )
        adapters: dict[str, QueueAdapter] = {
            **_stub_adapters(),
            "controller": QueueAdapter(
                "controller",
                "codex",
                "gpt-5.6-luna",
                [{"_raw_text": "This isn't JSON at all and never will be."}],
            ),
        }
        with patch(
            "memtrace_harness.cli_process.CliProcessRunner.run",
            return_value=ProcessResult(
                command=["claude"],
                return_code=1,
                stdout="",
                stderr="error",
                started_at="2026-09-05T00:00:00+00:00",
                completed_at="2026-09-05T00:00:01+00:00",
                duration_ms=100,
            ),
        ):
            summary = AgentLoopRunner(
                adapters=adapters,
                trace_store=TraceStore(self.db_path),
                config=cast(Any, _FakeChatConfig()),
            ).run(task)

        self.assertEqual(summary.status, "needs_human")
        self.assertEqual(summary.stages[0].state, "invalid_output")


class TieredGateTests(TestCase):
    def setUp(self) -> None:
        self.task = TaskEnvelope(task_id="t", workspace_id="w", goal="g", context_refs=[], context_items=[], constraints=[])

    def test_planners_tier_criteria_and_revisions_only_fix_blocking_findings(self) -> None:
        from memtrace_harness.loop import planner_escalation_task, planner_revision_task, planner_task

        self.assertIn("[必要]", planner_task(self.task).goal)
        for goal in (
            planner_revision_task(self.task, {}, {}).goal,
            planner_escalation_task(self.task, {}, {}).goal,
        ):
            self.assertIn("ONLY for the blocking findings", goal)

    def test_g1_gates_required_items_only_and_tightens_on_recheck(self) -> None:
        first = gate_task(self.task, "G1", {}).goal
        recheck = gate_task(self.task, "G1", {}, tighten=True).goal
        self.assertIn("Gate ONLY the '[必要]'", first)
        self.assertNotIn("re-check after the plan was already revised", first)
        self.assertIn("re-check after the plan was already revised", recheck)
        self.assertNotIn("re-check", gate_task(self.task, "G2", {}, {}, tighten=True).goal)

    def test_deferred_findings_reach_developer_and_g2_but_not_blocking_ones(self) -> None:
        from memtrace_harness.loop import deferred_findings, developer_task

        gate = {
            "verdict": "PASS",
            "findings": [
                {"severity": "defer: edge", "description": "empty caption", "required_action": "test it"},
                {"severity": "medium", "description": "naming", "required_action": ""},
                {"severity": "high", "description": "secret in log", "required_action": "mask"},
                {"severity": "defer: edge", "description": "empty caption", "required_action": "dup"},
            ],
        }
        rejected = {"verdict": "REJECT", "findings": [{"severity": "medium", "description": "blocking", "required_action": ""}]}
        notes = deferred_findings(rejected, gate)
        self.assertEqual([n["note"] for n in notes], ["empty caption", "naming"])
        self.assertIn("deferred-notes", developer_task(self.task, {}, notes).context_items[-1].ref)
        self.assertIn("deferred-notes", gate_task(self.task, "G2", {}, {}, deferred=notes).goal)
        self.assertEqual(deferred_findings(None, {"verdict": "REJECT", "findings": "x"}), [])
