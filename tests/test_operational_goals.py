from __future__ import annotations

import json
import subprocess
import threading
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import plans
from memtrace_harness.config import HarnessConfig
from memtrace_harness.scope import ProjectScope
from memtrace_harness.service import HarnessService
from memtrace_harness.trace_store import TraceStore


class OperationalGoalTests(TestCase):
    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.sqlite3"
        self.store = TraceStore(self.db_path)
        self.config = HarnessConfig(
            memtrace_mcp_url=None,
            memtrace_api_token=None,
            trace_db_path=self.db_path,
            trace_root=self.tmp,
            claude_command="claude",
            codex_command="codex",
            antigravity_command="agy",
            antigravity_output_mode="auto",
            cli_timeout_seconds=900,
            chat_provider="claude",
            chat_model="haiku",
            unattended_write_requires_approval=True,
        )

        self.scope_file = self.tmp / "harness-scope.md"
        self.scope_file.write_text("- workspace_id: ws_cdmp\n- working_directory: " + str(self.tmp) + "\n", encoding="utf-8")
        self.scope = ProjectScope(
            name="test_cdmp",
            workspace_id="ws_cdmp",
            working_directory=self.tmp,
            scope_file_path=self.scope_file,
            jobs={
                "cdmp_api_pipeline": "glab ci list -b dev",
                "cdmp_mp_api_pipeline": "glab ci list -b dev -R cdmp_mp_api",
            },
        )
        from memtrace_harness.approval import ApprovalManager
        from memtrace_harness.chat_triage import ChatTriage
        from memtrace_harness.primary_session import PrimarySessionManager

        self.approval_mgr = ApprovalManager(self.store)
        self.triage = ChatTriage([self.scope])
        self.psess_mgr = PrimarySessionManager(self.store)
        self.gateway = HarnessService(self.config, self.approval_mgr, self.triage, self.psess_mgr, [self.scope])
        self.gateway.send_message = MagicMock()
        self.gateway.send_message_with_keyboard = MagicMock()
        self.gateway.edit_message = MagicMock()

    def _drive(self, rounds: int = 6, confirm: bool = False) -> None:
        for _ in range(rounds):
            self.gateway.plan_runner.tick()
            self.gateway.process_job_requests()
            if confirm:
                with self.store._connection() as conn:
                    rows = [r[0] for r in conn.execute("SELECT request_id FROM job_requests WHERE status = 'awaiting'")]
                for rid in rows:
                    self.gateway._resolve_job_action("job_confirm", rid, 12345, 77)
            for thread in [t for t in threading.enumerate() if t.name.startswith(("job-", "plan-"))]:
                thread.join(10)

    def test_injected_cli_failures_and_command_corrections(self) -> None:
        """Test injected CLI outputs for missing executable and invalid-flag failures,
        verifying goal preservation, command correction, and final success requiring both repos.
        """
        goal_text = "查詢 cdmp_api 與 cdmp_mp_api 的最新 dev 管線與 commit 比對"
        criteria = [
            "cdmp_api origin/dev 管線及 commit 4db1fe5 比對",
            "cdmp_mp_api origin/dev 管線及 commit 2c3479c 比對",
        ]
        plan_payload = json.dumps({
            "goal": goal_text,
            "criteria": criteria,
            "steps": [
                {
                    "title": "查詢 cdmp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "4db1fe5"},
                },
                {
                    "title": "查詢 cdmp_mp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_mp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "2c3479c"},
                },
            ],
        }, ensure_ascii=False)

        # 1. Create plan
        res = self.gateway.plan_runner.create(self.scope, 12345, plan_payload)
        self.assertIsNone(res)
        active_plans = self.store.list_active_plans()
        self.assertEqual(len(active_plans), 1)
        plan_id = active_plans[0]["plan_id"]
        self.assertEqual(active_plans[0]["goal"], goal_text)
        self.assertEqual(active_plans[0]["criteria"], criteria)

        # 2. Simulate failure 1: missing executable (exit code 127)
        with patch.object(self.gateway, "_exec_job_command", return_value=("failed", 127, "glab: command not found")):
            self._drive(rounds=4, confirm=True)

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "needs_attention")
        steps = self.store.list_plan_steps(plan_id)
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("glab: command not found", steps[0]["note"])
        # Original goal and criteria preserved
        self.assertEqual(plan["goal"], goal_text)
        self.assertEqual(plan["criteria"], criteria)

        # 3. Simulate failure 2: switch to executable path, but -b dev timestamp error (exit code 1)
        corrected_payload_1 = json.dumps({
            "goal": goal_text,
            "steps": [
                {
                    "title": "查詢 cdmp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_api_pipeline", "command": "/usr/local/bin/glab ci list -b dev"},
                    "expect": {"kind": "commit_alignment", "value": "4db1fe5"},
                },
                {
                    "title": "查詢 cdmp_mp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_mp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "2c3479c"},
                },
            ],
        }, ensure_ascii=False)
        # Calling create() while plan is in needs_attention revises the plan without creating duplicate
        self.assertIsNone(self.gateway.plan_runner.create(self.scope, 12345, corrected_payload_1))
        # Ensure still only 1 active plan and same plan_id
        active_plans = self.store.list_active_plans()
        self.assertEqual(len(active_plans), 1)
        self.assertEqual(active_plans[0]["plan_id"], plan_id)

        with patch.object(self.gateway, "_exec_job_command", return_value=("failed", 1, "glab: -b dev: cannot parse timestamp")):
            self._drive(rounds=4, confirm=True)

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "needs_attention")
        steps = self.store.list_plan_steps(plan_id)
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("cannot parse timestamp", steps[0]["note"])

        # 4. Simulate failure 3: try --branch dev, unsupported flag (exit code 1)
        self.gateway.plan_runner.revise_failed_step(
            plan_id,
            tool="run_job",
            args={"name": "cdmp_api_pipeline", "command": "/usr/local/bin/glab ci list --branch dev"},
            title="查詢 cdmp_api 管線",
            expect={"kind": "commit_alignment", "value": "4db1fe5"},
        )
        with patch.object(self.gateway, "_exec_job_command", return_value=("failed", 1, "unknown flag: --branch dev")):
            self._drive(rounds=4, confirm=True)

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "needs_attention")
        steps = self.store.list_plan_steps(plan_id)
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("unknown flag: --branch dev", steps[0]["note"])

        # 5. Correct command with --ref dev and run both repos
        self.gateway.plan_runner.revise_failed_step(
            plan_id,
            tool="run_job",
            args={"name": "cdmp_api_pipeline", "command": "/usr/local/bin/glab ci list --ref dev"},
            title="查詢 cdmp_api 管線",
            expect={"kind": "commit_alignment", "value": "4db1fe5"},
        )

        def mock_exec(scope, command):
            if "cdmp_mp_api" in command:
                return ("done", 0, "Pipeline #102 passed on origin/dev (commit 2c3479c)")
            return ("done", 0, "Pipeline #101 passed on origin/dev (commit 4db1fe5)")

        # Also update cdmp_mp_api command with --ref dev
        self.gateway.plan_runner.revise_step(
            plan_id,
            idx=2,
            args={"name": "cdmp_mp_api_pipeline", "command": "/usr/local/bin/glab ci list --ref dev -R cdmp_mp_api"},
        )

        with patch.object(self.gateway, "_exec_job_command", side_effect=mock_exec):
            self._drive(rounds=6, confirm=True)

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "done")
        steps = self.store.list_plan_steps(plan_id)
        self.assertEqual([s["status"] for s in steps], ["done", "done"])
        self.assertEqual(plans.verdict(steps, plan["criteria"]), "達成")

        # Verify final message sent contains "達成" and no unresolved criteria
        final_msgs = [c.args[1] for c in self.gateway.send_message.call_args_list if "計畫結束：達成" in str(c.args[1:])]
        self.assertTrue(final_msgs)
        self.assertIn("目標：" + goal_text, final_msgs[0])
        self.assertNotIn("未滿足條件", final_msgs[0])

    def test_multi_repo_requires_both_outputs_and_commit_alignment(self) -> None:
        """Verify final success requires observed output for both repos, including pipeline status and commit alignment."""
        goal_text = "比對 cdmp 雙 repo 管線"
        criteria = [
            "cdmp_api origin/dev 管線及 commit 4db1fe5 比對",
            "cdmp_mp_api origin/dev 管線及 commit 2c3479c 比對",
        ]
        plan_payload = json.dumps({
            "goal": goal_text,
            "criteria": criteria,
            "steps": [
                {
                    "title": "查詢 cdmp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "4db1fe5"},
                },
                {
                    "title": "查詢 cdmp_mp_api 管線",
                    "tool": "run_job",
                    "args": {"name": "cdmp_mp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "2c3479c"},
                },
            ],
        }, ensure_ascii=False)

        self.assertIsNone(self.gateway.plan_runner.create(self.scope, 12345, plan_payload))
        plan_id = self.store.list_active_plans()[0]["plan_id"]

        # Only repo 1 succeeds, repo 2 returns wrong commit sha (9999999 instead of 2c3479c)
        def mock_exec(scope, command):
            if "cdmp_mp_api" in command:
                return ("done", 0, "Pipeline #102 passed on origin/dev (commit 9999999)")
            return ("done", 0, "Pipeline #101 passed on origin/dev (commit 4db1fe5)")

        with patch.object(self.gateway, "_exec_job_command", side_effect=mock_exec):
            self._drive(rounds=6, confirm=True)

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "needs_attention")
        steps = self.store.list_plan_steps(plan_id)
        # Step 1 is done, step 2 failed because commit did not match 2c3479c
        self.assertEqual(steps[0]["status"], "done")
        self.assertEqual(steps[1]["status"], "failed")
        self.assertIn("未包含預期 commit", steps[1]["note"])

        # Check verdict is not "達成"
        self.assertNotEqual(plans.verdict(steps, plan["criteria"]), "達成")

        # Check render_final reports unresolved criteria and concrete error
        final_text = plans.render_final(plan, steps)
        self.assertIn("未達成", final_text)
        self.assertIn("未滿足條件：cdmp_mp_api origin/dev 管線及 commit 2c3479c 比對", final_text)
        self.assertIn("最後具體錯誤：", final_text)

    def test_progress_messages_label_setup_exec_and_readback(self) -> None:
        """Verify progress messages label plan/job setup separately from actual execution and result readback."""
        steps = [
            {"status": plans.AWAITING, "title": "新增管線查詢工作", "tool": "propose_scope_change"},
            {"status": plans.RUNNING, "title": "執行 cdmp_api 查詢", "tool": "run_job"},
            {"status": plans.CHECKING, "title": "讀取 cdmp_api 結果", "tool": "read_file"},
        ]
        plan = {"plan_id": "p1", "goal": "測試階段標籤", "status": plans.P_RUNNING}

        # 1. Step 0 is AWAITING with propose_scope_change -> should show 工作設定：等你確認
        text, _ = plans.render_progress(plan, steps)
        self.assertIn("新增管線查詢工作（工作設定：等你確認）", text)
        self.assertIn("執行 cdmp_api 查詢（執行中）", text)
        self.assertIn("讀取 cdmp_api 結果（讀取結果中）", text)

        # 2. Step 0 is RUNNING with propose_scope_change -> should show 工作設定中
        steps[0]["status"] = plans.RUNNING
        text2, _ = plans.render_progress(plan, steps)
        self.assertIn("新增管線查詢工作（工作設定中）", text2)

        # 3. Explicit stage attribute overrides tool inference
        steps[1]["stage"] = "readback"
        text3, _ = plans.render_progress(plan, steps)
        self.assertIn("執行 cdmp_api 查詢（讀取結果中）", text3)

    def test_exhausted_retries_and_cancellation_report_unresolved_criteria_and_last_error(self) -> None:
        """If retries are exhausted or the task is cancelled, report the unresolved criteria and the last concrete error."""
        criteria = [
            "cdmp_api origin/dev 管線及 commit 4db1fe5 比對",
            "cdmp_mp_api origin/dev 管線及 commit 2c3479c 比對",
        ]
        plan_payload = json.dumps({
            "goal": "查詢管線",
            "criteria": criteria,
            "steps": [
                {
                    "title": "查詢 cdmp_api",
                    "tool": "run_job",
                    "args": {"name": "cdmp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "4db1fe5"},
                },
                {
                    "title": "查詢 cdmp_mp_api",
                    "tool": "run_job",
                    "args": {"name": "cdmp_mp_api_pipeline"},
                    "expect": {"kind": "commit_alignment", "value": "2c3479c"},
                },
            ],
        }, ensure_ascii=False)

        self.assertIsNone(self.gateway.plan_runner.create(self.scope, 12345, plan_payload))
        plan_id = self.store.list_active_plans()[0]["plan_id"]

        with patch.object(self.gateway, "_exec_job_command", return_value=("failed", 127, "glab: command not found")):
            self._drive(rounds=4, confirm=True)

        # User cancels plan
        reply = self.gateway.plan_runner.handle_action("plan_cancel", plan_id)
        self.assertEqual(reply, "已取消計畫。")

        plan = self.store.get_plan(plan_id)
        self.assertEqual(plan["status"], "cancelled")

        # Find the final message sent to user
        final_msgs = [c.args[1] for c in self.gateway.send_message.call_args_list if "計畫結束：未達成" in str(c.args[1:])]
        self.assertTrue(final_msgs)
        final_text = final_msgs[-1]
        self.assertIn("未滿足條件：", final_text)
        self.assertIn("cdmp_api origin/dev 管線及 commit 4db1fe5 比對", final_text)
        self.assertIn("最後具體錯誤：glab: command not found", final_text)

    def test_scope_change_omitted_kind_defaults_to_job(self) -> None:
        """Verify propose_scope_change in a plan defaults kind='job' when command is provided."""
        plan_payload = json.dumps({
            "goal": "新增並執行 job",
            "steps": [
                {
                    "title": "新增 glab_query 工作",
                    "tool": "propose_scope_change",
                    "args": {"name": "glab_query", "command": "echo pipeline_ok"},
                },
            ],
        }, ensure_ascii=False)
        self.assertIsNone(self.gateway.plan_runner.create(self.scope, 12345, plan_payload))
        plan_id = self.store.list_active_plans()[0]["plan_id"]
        # Driving step 1 proposes scope change
        self.gateway.plan_runner.tick()
        steps = self.store.list_plan_steps(plan_id)
        # Should be awaiting user confirmation, not failed
        self.assertEqual(steps[0]["status"], "awaiting")
        # Job request should have change with kind=job
        req = self.store.get_job_request(steps[0]["request_id"])
        self.assertIsNotNone(req)
        self.assertEqual(req["payload"]["kind"], "job")
        self.assertEqual(req["payload"]["name"], "glab_query")
