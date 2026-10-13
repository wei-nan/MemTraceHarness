from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.loop import AgentLoopRunner, controller_task, phase_context_items
from memtrace_harness.goal_plan import Phase
from memtrace_harness.schemas import TaskEnvelope
from memtrace_harness.trace_store import TraceStore

from tests.test_loop import QueueAdapter, completed_development, passed_gate, ready_plan

BUILD = {"title": "Build", "purpose": "add the button", "skills": ["plan", "develop"], "acceptance": ["exports CSV"]}
DOCS = {"title": "Docs", "purpose": "document it", "skills": ["develop"], "acceptance": ["README updated"]}
DESIGN = {"title": "Design", "purpose": "decide the layout", "skills": ["plan"], "acceptance": []}


def goal_plan(*phases: dict) -> dict:
    return {
        "restated_goal": "add the export button and document it",
        "success_criteria": ["button exports CSV", "README explains it"],
        "phases": list(phases),
    }


def start(*phases: dict) -> dict:
    return {"action": "run_planner", "reason": "needs work", "goal_plan": goal_plan(*phases)}


def check(*statuses: str, verdict: str = "met", remaining: list[str] | None = None) -> dict:
    return {
        "verdict": verdict,
        "criteria": [
            {"criterion": name, "status": status, "evidence": "shown by the stage results"}
            for name, status in zip(("button exports CSV", "README explains it"), statuses)
        ],
        "remaining": remaining or [],
    }


def finish(*statuses: str, **kwargs) -> dict:
    return {"action": "finish", "reason": "done", "goal_check": check(*statuses, **kwargs)}


class GoalPlanRunTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "trace.sqlite3"
        self.task = TaskEnvelope(task_id="task_x", workspace_id="ws_x", goal="add the export button and document it")

    def adapters(self, controller, plan=(), review=(), develop=()) -> dict[str, QueueAdapter]:
        return {
            "controller": QueueAdapter("controller", "codex", "gpt-5.6-luna", list(controller)),
            "plan": QueueAdapter("plan", "claude", "sonnet", list(plan)),
            "plan-escalation": QueueAdapter("plan-escalation", "claude", "opus", []),
            "review": QueueAdapter("review", "codex", "gpt-5.6-sol", list(review)),
            "develop": QueueAdapter("develop", "antigravity", "gemini-3.8-flash-high", list(develop)),
        }

    def run_loop(self, adapters, conversation_id=None):
        runner = AgentLoopRunner(adapters=adapters, trace_store=TraceStore(self.db_path))
        return runner.run(self.task, conversation_id=conversation_id)

    def test_two_phases_run_in_order_with_a_progress_check_between_and_a_goal_report_at_the_end(self) -> None:
        adapters = self.adapters(
            controller=[start(BUILD, DOCS), {"action": "next_phase", "reason": "on track", "goal_check": check("met", "unmet", verdict="partly_met")}, finish("met", "met")],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate(), passed_gate()],
            develop=[completed_development(), completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(
            [stage.stage for stage in summary.stages],
            ["control-start", "plan", "g1", "develop", "g2", "progress-1", "p2-develop", "p2-g2", "converge"],
        )
        self.assertIn("✅ button exports CSV", summary.recommendation)
        self.assertIn("✅ README explains it", summary.recommendation)

    def test_a_task_is_not_called_done_while_a_success_criterion_is_unmet(self) -> None:
        adapters = self.adapters(
            controller=[start(BUILD), finish("met", "unmet", verdict="partly_met", remaining=["document it"])],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("只完成了一部分", summary.recommendation)
        self.assertIn("document it", summary.recommendation)

    def test_finishing_without_any_goal_check_is_refused_once_criteria_were_written(self) -> None:
        adapters = self.adapters(
            controller=[start(BUILD), {"action": "finish", "reason": "done"}],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("只完成了一部分", summary.recommendation)

    def test_a_goal_that_is_not_met_can_be_extended_with_more_phases_instead_of_stopping_half_done(self) -> None:
        adapters = self.adapters(
            controller=[
                start(BUILD),
                {
                    "action": "revise_phases",
                    "reason": "docs are still missing",
                    "goal_check": check("met", "unmet", verdict="partly_met"),
                    "phases": [DOCS],
                },
                finish("met", "met"),
            ],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate(), passed_gate()],
            develop=[completed_development(), completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(
            [stage.stage for stage in summary.stages][-4:], ["converge", "p2-develop", "p2-g2", "converge-2"]
        )

    def test_unusable_extra_phases_stop_for_a_human(self) -> None:
        adapters = self.adapters(
            controller=[
                start(BUILD),
                {"action": "revise_phases", "reason": "more", "goal_check": check("met", "unmet", verdict="partly_met"), "phases": [{"title": "x"}]},
            ],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "needs_human")
        self.assertIn("could use", summary.recommendation)

    def test_the_number_of_phases_is_bounded(self) -> None:
        one = {**DOCS}
        adapters = self.adapters(
            controller=[
                start(*[one] * 6),
                *[{"action": "next_phase", "reason": "go"}] * 5,
                {"action": "revise_phases", "reason": "still more", "goal_check": check("met", "unmet"), "phases": [one]},
            ],
            review=[passed_gate()] * 8,
            develop=[completed_development()] * 8,
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "needs_human")
        self.assertEqual(sum(1 for stage in summary.stages if stage.profile_id == "develop"), 6)

    def test_a_develop_only_phase_skips_plan_and_its_gate_but_is_still_reviewed(self) -> None:
        adapters = self.adapters(
            controller=[start(DOCS), finish("met", "met")],
            review=[passed_gate()],
            develop=[completed_development()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual([stage.stage for stage in summary.stages], ["control-start", "develop", "g2", "converge"])
        self.assertEqual(adapters["plan"].calls, [])
        assigned = [item for item in adapters["develop"].calls[0].context_items if item.ref == "harness:accepted-plan"]
        self.assertIn("assigned_by", assigned[0].body)

    def test_a_plan_only_phase_never_reaches_develop(self) -> None:
        adapters = self.adapters(
            controller=[start(DESIGN), finish("met", "met")],
            plan=[ready_plan()],
            review=[passed_gate()],
        )
        summary = self.run_loop(adapters)

        self.assertEqual(summary.status, "succeeded")
        self.assertEqual([stage.stage for stage in summary.stages], ["control-start", "plan", "g1", "converge"])
        self.assertEqual(adapters["develop"].calls, [])

    def test_review_gets_no_say_in_which_skills_a_phase_uses(self) -> None:
        adapters = self.adapters(
            controller=[start({**BUILD, "skills": ["review"]}), finish("met", "met")],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        summary = self.run_loop(adapters)

        # the unusable plan falls back to the default plan-then-develop phase, and no criteria were recorded
        self.assertEqual([stage.stage for stage in summary.stages][:5], ["control-start", "plan", "g1", "develop", "g2"])

    def test_a_resumed_conversation_reuses_settled_phases_and_continues_with_the_next(self) -> None:
        first = self.adapters(
            controller=[start(BUILD, DOCS), {"action": "ask_human", "reason": "which README section?"}],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        stopped = self.run_loop(first)
        self.assertEqual(stopped.status, "needs_human")

        second = self.adapters(
            controller=[start(BUILD, DOCS), {"action": "next_phase", "reason": "ok"}, finish("met", "met")],
            review=[passed_gate()],
            develop=[completed_development()],
        )
        resumed = self.run_loop(second, conversation_id=stopped.conversation_id)

        self.assertEqual(resumed.status, "succeeded")
        self.assertEqual(second["plan"].calls, [])  # phase 1 was reused whole
        self.assertEqual(len(second["develop"].calls), 1)  # only phase 2's develop ran
        self.assertEqual(len(second["review"].calls), 1)

    def test_a_replanned_goal_does_not_inherit_artifacts_of_a_different_phase(self) -> None:
        first = self.adapters(
            controller=[start(BUILD), finish("met", "unmet", verdict="partly_met")],
            plan=[ready_plan()],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        stopped = self.run_loop(first)

        different = {**BUILD, "title": "Build something else"}
        second = self.adapters(
            controller=[start(different), finish("met", "met")],
            plan=[ready_plan("new plan")],
            review=[passed_gate(), passed_gate()],
            develop=[completed_development()],
        )
        self.run_loop(second, conversation_id=stopped.conversation_id)

        self.assertEqual(len(second["plan"].calls), 1)
        self.assertEqual(len(second["develop"].calls), 1)


class ControllerPromptTests(TestCase):
    task = TaskEnvelope(task_id="t", workspace_id="w", goal="do it")

    def test_start_asks_for_a_goal_plan_and_lists_the_skills(self) -> None:
        goal = controller_task(self.task, stage="start", stages=[]).goal
        self.assertIn("goal_plan", goal)
        self.assertIn("success_criteria", goal)
        self.assertIn("- develop (workspace-write)", goal)
        self.assertIn("- review (read-only)", goal)
        self.assertIn("Review is never listed", goal)

    def test_a_project_without_the_agent_loop_is_not_asked_for_a_plan(self) -> None:
        goal = controller_task(self.task, stage="start", stages=[], agent_loop_enabled=False).goal
        self.assertNotIn("Skills you can use", goal)

    def test_converge_demands_a_per_criterion_check_and_offers_to_continue(self) -> None:
        goal = controller_task(self.task, stage="converge", stages=[], can_revise=True).goal
        self.assertIn("goal_check", goal)
        self.assertIn("revise_phases", goal)
        self.assertIn("['finish', 'ask_human', 'revise_phases']", goal)
        no_more = controller_task(self.task, stage="converge", stages=[], can_revise=False).goal
        self.assertIn("['finish', 'ask_human']", no_more)

    def test_progress_offers_next_phase(self) -> None:
        goal = controller_task(self.task, stage="progress", stages=[]).goal
        self.assertIn("['next_phase', 'revise_phases', 'finish', 'ask_human']", goal)
        self.assertIn("kb_updates must be null", goal)

    def test_the_users_own_words_reach_the_controller(self) -> None:
        from memtrace_harness.loop import USER_REQUEST_CONTENT_TYPE
        from memtrace_harness.schemas import ContextItem

        task = TaskEnvelope(
            task_id="t", workspace_id="w", goal="short",
            context_items=[ContextItem(ref="harness:user-request", title="r", body="the long original", content_type=USER_REQUEST_CONTENT_TYPE)],
        )
        refs = [item.ref for item in controller_task(task, stage="start", stages=[]).context_items]
        self.assertIn("harness:user-request", refs)

    def test_the_default_single_phase_adds_nothing_to_the_stage_prompts(self) -> None:
        self.assertEqual(phase_context_items(Phase("t", "p", ("plan", "develop")), 1, 1, []), [])
        self.assertEqual(len(phase_context_items(Phase("t", "p", ("develop",), ("a",)), 2, 3, [])), 1)


class UserRequestTests(TestCase):
    def test_the_users_original_words_follow_a_conversation_into_its_resumed_runs(self) -> None:
        from memtrace_harness.loop import user_request_item

        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            task = TaskEnvelope(
                task_id="t", workspace_id="w", goal="one line summary",
                context_items=[user_request_item("加匯出按鈕，再把使用方式寫進 README")],
            )
            adapters = {
                "controller": QueueAdapter("controller", "codex", "gpt-5.6-luna", [
                    {"action": "ask_human", "reason": "first"}, {"action": "ask_human", "reason": "second"},
                ]),
                "plan": QueueAdapter("plan", "claude", "sonnet", []),
                "plan-escalation": QueueAdapter("plan-escalation", "claude", "opus", []),
                "review": QueueAdapter("review", "codex", "gpt-5.6-sol", []),
                "develop": QueueAdapter("develop", "antigravity", "gemini-3.8-flash-high", []),
            }
            first = AgentLoopRunner(adapters=adapters, trace_store=store).run(task)
            resumed_task = TaskEnvelope(task_id="t", workspace_id="w", goal="one line summary\n\nHuman clarification: yes")
            AgentLoopRunner(adapters=adapters, trace_store=store).run(
                resumed_task, conversation_id=first.conversation_id
            )

            seen = adapters["controller"].calls[1]
            bodies = [item.body for item in seen.context_items if item.content_type == "user_request"]
            self.assertEqual(bodies, ["加匯出按鈕，再把使用方式寫進 README"])
            self.assertEqual(store.conversation_user_request("never_seen"), None)


class StageLabelTests(TestCase):
    def test_later_phase_stages_read_as_the_plain_stage_with_its_phase(self) -> None:
        from memtrace_harness.service import HarnessService

        label = HarnessService._stage_label
        self.assertEqual(label("develop"), "develop")
        self.assertEqual(label("p2-develop"), "develop（第 2 階段）")
        self.assertEqual(label("progress-1"), "階段檢查 1")
        self.assertEqual(label("converge-2"), "收尾 2")
        self.assertEqual(label("something-new"), "something-new")
