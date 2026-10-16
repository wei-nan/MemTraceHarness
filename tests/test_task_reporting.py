from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.loop import stage_task
from memtrace_harness.output_contracts import output_schema_path
from memtrace_harness.schemas import LoopSummary, TaskEnvelope, TokenUsage
from memtrace_harness.task_reporting import StageNarrator, outcome_message
from tests.test_service import _build_schedule_gateway
from tempfile import TemporaryDirectory


def stage(name, artifact):
    return SimpleNamespace(stage=name, artifact=artifact)


class TaskReportingTests(TestCase):
    def test_start_explains_intent_and_end_uses_actual_short_result(self):
        narrator = StageNarrator()
        self.assertIn("需求", narrator.render("control-start", "running", None))
        planned = narrator.render("control-start", "succeeded", {
            "action": "run_planner", "goal_plan": {"phases": [
                {"title": "修復邀請問題", "purpose": "找出邀請失敗原因並修正"},
            ]},
        })
        self.assertIn("修復邀請問題", planned)
        self.assertIn("找出邀請失敗原因並修正", narrator.render("develop", "running", None))
        done = narrator.render("develop", "succeeded", {
            "status": "completed", "summary": "technical details " * 100,
            "user_summary": "已修正邀請的錯誤提示，接著重新檢查。",
        })
        self.assertIn("已修正邀請的錯誤提示", done)
        self.assertNotIn("technical", done)
        self.assertLess(len(done), 200)

    def test_each_phase_and_its_revision_keep_the_current_intent(self):
        narrator = StageNarrator()
        text = narrator.render("p2-develop", "running", {"phase": {"purpose": "修正第二個專案的邀請畫面"}})
        self.assertIn("第二個專案", text)
        self.assertIn("第二個專案", narrator.render("p2-develop-revision", "running", None))

    def test_process_success_does_not_turn_failed_or_unrun_checks_into_success(self):
        narrator = StageNarrator()
        failed = narrator.render("develop", "succeeded", {"status": "needs_human", "user_summary": "還缺少一項需求。"})
        self.assertIn("還有問題", failed)
        self.assertNotIn("已完成", failed)
        for status in ("regression", "could_not_run", "baseline_failing"):
            with self.subTest(status=status):
                text = narrator.render("verify", "succeeded", {"verification_status": status})
                self.assertNotIn("已通過", text)
        mixed = narrator.render("verify", "succeeded", {
            "verification_status": "passed", "repo_verifications": [
                {"repo": "main", "status": "passed"}, {"repo": "android", "status": "could_not_run"},
            ],
        })
        self.assertIn("未能執行", mixed)

    def test_converge_does_not_claim_goal_or_merge_before_runtime_decides(self):
        text = StageNarrator().render("converge", "succeeded", {
            "action": "merge", "user_summary": "全部完成，已合併。",
        })
        self.assertNotIn("已合併", text)
        self.assertNotIn("全部完成", text)

    def test_partial_merge_reports_which_repos_are_still_pending(self):
        text = StageNarrator().render("integrate", "failed", {"delivery": {
            "state": "partial", "merged_repos": ["main"], "pending_repos": ["android"],
        }})
        self.assertIn("已整合：main", text)
        self.assertIn("仍待整合：android", text)

    def test_final_report_is_short_and_preserves_full_durable_evidence(self):
        recommendation = "目標及每一項驗收證據\n" * 200
        summary = LoopSummary(
            TaskEnvelope("t", "ws", "修復邀請"), [
                stage("develop", {"status": "completed", "user_summary": "已修正邀請的錯誤提示。", "gaps": []}),
                stage("verify", {"verification_status": "regression"}),
                stage("verify-recheck", {"verification_status": "passed"}),
                stage("converge", {"action": "merge", "user_summary": "邀請流程已修正，重新檢查也通過。"}),
            ], "succeeded", recommendation, "trace", TokenUsage(), delivery={"state": "merged", "target_branch": "main"},
        )
        text = outcome_message("Beri", summary)
        self.assertLess(len(text), 250)
        self.assertIn("邀請流程已修正", text)
        self.assertIn("檢查已通過", text)
        self.assertIn("本機主分支", text)
        self.assertNotIn("還沒通過", text)
        self.assertNotIn("驗收證據", text)
        self.assertEqual(summary.recommendation, recommendation)

    def test_successful_run_still_mentions_unverified_checks_and_unmerged_work(self):
        summary = SimpleNamespace(status="succeeded", recommendation="full evidence", stages=[
            stage("develop", {"user_summary": "已調整畫面。", "gaps": ["雙裝置操作仍待確認"]}),
            stage("verify", {"verification_status": "could_not_run"}),
        ], delivery={"state": "unmerged"})
        text = outcome_message("Beri", summary)
        self.assertIn("未能執行", text)
        self.assertIn("尚待整合", text)
        self.assertIn("雙裝置", text)

    def test_legacy_artifacts_without_user_summary_still_have_a_bounded_report(self):
        summary = SimpleNamespace(status="succeeded", recommendation="long evidence" * 300,
                                  stages=[stage("develop", {"summary": "已調整邀請。" * 100})])
        self.assertLess(len(outcome_message("Beri", summary)), 240)

    def test_same_model_call_can_supply_plain_summary_without_changing_validation_fields(self):
        task = stage_task(TaskEnvelope("t", "ws", "g"), suffix="develop", goal="do work", context_items=[])
        self.assertIn("user_summary", task.goal)
        self.assertIn("without engineering knowledge", task.goal)
        for profile in ("controller", "plan", "review", "develop"):
            schema = json.loads(output_schema_path(profile).read_text())
            self.assertIn("user_summary", schema["properties"])
            self.assertIn("user_summary", schema["required"])  # CLI strict schema requires every property
            self.assertIn("null", schema["properties"]["user_summary"]["type"])

    def test_progress_is_saved_and_delivered_with_the_same_plain_wording(self):
        with TemporaryDirectory() as directory:
            gateway, store, scope = _build_schedule_gateway(Path(directory))
            gateway.send_message = MagicMock()
            report = gateway._agent_progress(scope, "conversation", "audience")
            report("verify", "running", None)
            report("verify", "succeeded", {"verification_status": "passed"})
            messages = [call.args[1] for call in gateway.send_message.call_args_list]
            self.assertEqual(len(messages), 2)
            self.assertIn("重新執行檢查", messages[0])
            self.assertIn("檢查已通過", messages[1])
            self.assertFalse(any("verify" in text or "passed" in text for text in messages))
            turns = store.get_recent_primary_session_turns(f"psess_{scope.name}", limit=10)
            self.assertTrue(any(turn["content"] == messages[1] for turn in turns))
