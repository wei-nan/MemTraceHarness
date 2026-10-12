from __future__ import annotations

import json
import unittest

from memtrace_harness import plans


def _ok(name: str) -> str | None:
    return None if name in {"run_job", "read_file"} else "沒有這個工具"


def _plan(steps: list[dict], goal: str = "g") -> str:
    return json.dumps({"goal": goal, "steps": steps})


class ParseTests(unittest.TestCase):
    def test_a_good_plan_is_normalized_with_a_default_expectation(self) -> None:
        plan = plans.parse_plan(_plan([{"title": "t", "tool": "read_file", "args": {"path": "a"}}]), allowed_tool=_ok)
        self.assertEqual(plan["steps"][0]["expect"], {"kind": "ok", "value": ""})

    def test_a_bare_string_expectation_means_a_model_judges_it(self) -> None:
        plan = plans.parse_plan(_plan([{"title": "t", "tool": "read_file", "expect": "服務正常"}]), allowed_tool=_ok)
        self.assertEqual(plan["steps"][0]["expect"], {"kind": "model", "value": "服務正常"})

    def test_bad_plans_are_refused(self) -> None:
        for payload in [
            "not json", "[]", _plan([]), _plan([{"title": "t", "tool": "edit_code"}]),
            _plan([{"title": "", "tool": "read_file"}]),
            _plan([{"title": "t", "tool": "read_file"}] * (plans.MAX_STEPS + 1)),
            _plan([{"title": "t", "tool": "read_file", "expect": {"kind": "magic", "value": "x"}}]),
            _plan([{"title": "t", "tool": "read_file", "expect": {"kind": "regex", "value": "("}}]),
            _plan([{"title": "t", "tool": "read_file", "expect": {"kind": "contains"}}]),
            json.dumps({"goal": "", "steps": [{"title": "t", "tool": "read_file"}]}),
        ]:
            with self.assertRaises(plans.PlanError, msg=payload[:60]):
                plans.parse_plan(payload, allowed_tool=_ok)


class ExpectationTests(unittest.TestCase):
    def test_each_kind(self) -> None:
        self.assertEqual(plans.check_expectation({"kind": "ok"}, "")[0], True)
        self.assertEqual(plans.check_expectation({"kind": "contains", "value": "Run"}, "running")[0], True)
        self.assertEqual(plans.check_expectation({"kind": "not_contains", "value": "error"}, "all good")[0], True)
        self.assertEqual(plans.check_expectation({"kind": "not_contains", "value": "error"}, "an Error")[0], False)
        self.assertEqual(plans.check_expectation({"kind": "regex", "value": r"^\d+\s+0\s"}, "12 0 x")[0], True)
        self.assertIsNone(plans.check_expectation({"kind": "model", "value": "ok?"}, "x")[0])


class RenderTests(unittest.TestCase):
    def test_progress_shows_each_step_and_the_right_buttons(self) -> None:
        plan = {"plan_id": "p", "goal": "G", "status": plans.P_ATTENTION, "approve_all": False}
        steps = [
            {"status": plans.DONE, "title": "a"},
            {"status": plans.FAILED, "title": "b", "note": "沒達到預期"},
            {"status": plans.PENDING, "title": "c"},
        ]
        text, keyboard = plans.render_progress(plan, steps)
        self.assertIn("1/3", text.replace("（1/3）", "1/3"))
        self.assertIn("❌ 2. b", text)
        self.assertIn("沒達到預期", text)
        ids = [b["id"] for row in keyboard for b in row]
        self.assertEqual(ids, ["plan_retry:p", "plan_skip:p", "plan_approve_all:p", "plan_cancel:p"])

    def test_verdict(self) -> None:
        self.assertEqual(plans.verdict([{"status": plans.DONE}] * 2), "達成")
        self.assertIn("部分", plans.verdict([{"status": plans.DONE}, {"status": plans.SKIPPED}]))
        self.assertEqual(plans.verdict([{"status": plans.DONE}, {"status": plans.FAILED}]), "未達成")


if __name__ == "__main__":
    unittest.main()
