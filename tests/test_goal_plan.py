from __future__ import annotations

from unittest import TestCase

from memtrace_harness.goal_plan import (
    MAX_PHASES,
    GoalPlan,
    Phase,
    assignment_plan,
    default_goal_plan,
    judge_goal_check,
    parse_goal_plan,
    parse_phases,
    render_goal_report,
)


def plan_dict(**overrides) -> dict:
    base = {
        "restated_goal": "add the export button and document it",
        "success_criteria": ["button exports CSV", "README explains it"],
        "phases": [
            {"title": "Build", "purpose": "add the button", "skills": ["plan", "develop"], "acceptance": ["exports"]},
            {"title": "Docs", "purpose": "document it", "skills": ["develop"], "acceptance": []},
        ],
    }
    return {**base, **overrides}


def check_dict(*statuses: str, verdict: str = "met", remaining: list[str] | None = None) -> dict:
    return {
        "verdict": verdict,
        "criteria": [
            {"criterion": f"criterion {index}", "status": status, "evidence": "seen in the stage results"}
            for index, status in enumerate(statuses, 1)
        ],
        "remaining": remaining or [],
    }


class ParseGoalPlanTests(TestCase):
    def test_a_well_formed_plan_parses_with_its_phases_in_order(self) -> None:
        plan = parse_goal_plan(plan_dict())
        assert plan is not None
        self.assertEqual([phase.title for phase in plan.phases], ["Build", "Docs"])
        self.assertEqual(plan.phases[1].skills, ("develop",))
        self.assertEqual(len(plan.success_criteria), 2)
        self.assertFalse(plan.defaulted)

    def test_a_phase_may_plan_only_or_develop_only_but_never_review_or_nothing(self) -> None:
        self.assertIsNotNone(parse_phases([{"title": "t", "purpose": "p", "skills": ["plan"], "acceptance": []}]))
        for skills in (["review"], [], ["develop", "plan"], ["plan", "review"], ["plan", "develop", "develop"]):
            self.assertIsNone(
                parse_phases([{"title": "t", "purpose": "p", "skills": skills, "acceptance": []}]), skills
            )

    def test_a_malformed_plan_is_rejected_rather_than_half_used(self) -> None:
        self.assertIsNone(parse_goal_plan(None))
        self.assertIsNone(parse_goal_plan(plan_dict(success_criteria=[])))
        self.assertIsNone(parse_goal_plan(plan_dict(restated_goal="")))
        self.assertIsNone(parse_goal_plan(plan_dict(phases=[])))
        self.assertIsNone(parse_goal_plan(plan_dict(phases=[{"title": "t"}])))

    def test_the_phase_count_is_capped(self) -> None:
        phase = {"title": "t", "purpose": "p", "skills": ["develop"], "acceptance": []}
        self.assertIsNotNone(parse_phases([phase] * MAX_PHASES))
        self.assertIsNone(parse_phases([phase] * (MAX_PHASES + 1)))
        self.assertIsNone(parse_phases([phase] * 3, limit=2))

    def test_the_default_plan_is_one_plan_then_develop_phase_with_no_criteria(self) -> None:
        plan = default_goal_plan("fix the login bug")
        self.assertTrue(plan.defaulted)
        self.assertEqual(len(plan.phases), 1)
        self.assertEqual(plan.phases[0].skills, ("plan", "develop"))
        self.assertEqual(plan.success_criteria, ())

    def test_a_directly_assigned_phase_becomes_a_ready_plan_for_develop(self) -> None:
        plan = assignment_plan(Phase("Docs", "document the export", ("develop",), ("README mentions it",)))
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["acceptance_criteria"], ["README mentions it"])
        self.assertEqual(plan["assigned_by"], "controller")


class JudgeGoalCheckTests(TestCase):
    def setUp(self) -> None:
        self.plan = parse_goal_plan(plan_dict())

    def test_every_criterion_met_lets_the_task_finish(self) -> None:
        verdict = judge_goal_check(self.plan, check_dict("met", "met"))
        self.assertTrue(verdict.complete)

    def test_one_unmet_criterion_blocks_finishing_and_is_named(self) -> None:
        verdict = judge_goal_check(self.plan, check_dict("met", "unmet", verdict="partly_met"))
        self.assertFalse(verdict.complete)
        self.assertEqual(verdict.unmet, ("criterion 2",))

    def test_a_met_verdict_cannot_override_an_unmet_criterion(self) -> None:
        self.assertFalse(judge_goal_check(self.plan, check_dict("met", "unmet", verdict="met")).complete)

    def test_work_listed_as_remaining_blocks_finishing(self) -> None:
        verdict = judge_goal_check(self.plan, check_dict("met", "met", remaining=["update the changelog"]))
        self.assertFalse(verdict.complete)
        self.assertIn("update the changelog", verdict.unmet)

    def test_a_check_that_skips_a_success_criterion_does_not_count(self) -> None:
        verdict = judge_goal_check(self.plan, check_dict("met"))
        self.assertFalse(verdict.complete)
        self.assertIn("2", verdict.problem)

    def test_unverified_criteria_may_finish_but_are_reported(self) -> None:
        verdict = judge_goal_check(self.plan, check_dict("met", "unverified"))
        self.assertTrue(verdict.complete)
        self.assertEqual(verdict.unverified, ("criterion 2",))

    def test_a_missing_check_blocks_finishing_only_when_criteria_were_written(self) -> None:
        self.assertFalse(judge_goal_check(self.plan, None).complete)
        self.assertTrue(judge_goal_check(default_goal_plan("anything"), None).complete)
        self.assertTrue(judge_goal_check(None, None).complete)

    def test_a_garbled_check_is_refused(self) -> None:
        self.assertFalse(judge_goal_check(self.plan, {"verdict": "great", "criteria": []}).complete)
        self.assertFalse(judge_goal_check(self.plan, {"verdict": "met", "criteria": ["x", "y"]}).complete)
        bad = check_dict("met", "met")
        bad["criteria"][0]["status"] = "done"
        self.assertFalse(judge_goal_check(self.plan, bad).complete)


class RenderGoalReportTests(TestCase):
    def test_the_report_lists_each_criterion_with_its_state_and_evidence(self) -> None:
        plan = parse_goal_plan(plan_dict())
        check = check_dict("met", "unverified")
        text = render_goal_report(plan, check, judge_goal_check(plan, check))
        self.assertIn("目標：add the export button and document it", text)
        self.assertIn("✅ criterion 1（seen in the stage results）", text)
        self.assertIn("❔ criterion 2", text)
        self.assertIn("沒有被獨立驗證", text)

    def test_nothing_to_report_without_a_check(self) -> None:
        self.assertEqual(render_goal_report(None, None), "")
        self.assertEqual(render_goal_report(None, {"criteria": "no"}), "")


class GoalPlanSerializationTests(TestCase):
    def test_a_plan_round_trips_through_its_dict_form(self) -> None:
        plan = parse_goal_plan(plan_dict())
        assert plan is not None
        self.assertEqual(parse_goal_plan(plan.to_dict()), plan)
        self.assertIsInstance(plan, GoalPlan)


class ControllerSchemaTests(TestCase):
    def test_the_controller_schema_asks_for_the_goal_fields_in_strict_shape(self) -> None:
        import json

        from memtrace_harness.output_contracts import output_schema_path

        schema = json.loads(output_schema_path("controller").read_text(encoding="utf-8"))
        for field in ("goal_plan", "goal_check", "phases"):
            self.assertIn(field, schema["required"])
        for action in ("next_phase", "revise_phases"):
            self.assertIn(action, schema["properties"]["action"]["enum"])

        def check(node, path="$"):
            if isinstance(node, dict):
                if "properties" in node:
                    # strict structured-output mode: every property required, nothing extra
                    self.assertEqual(set(node.get("required", [])), set(node["properties"]), path)
                    self.assertIs(node.get("additionalProperties"), False, path)
                for key, value in node.items():
                    check(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    check(value, f"{path}[{index}]")

        check(schema)
