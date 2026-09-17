from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


class RedTeamAcceptanceTests(TestCase):
    def test_red_team_acceptance_allowlist_security(self) -> None:
        """Red Team Verification 1: Allowlist Security Boundary"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "rt_sec.sqlite3"
            trace_store = TraceStore(db_path)
            config = HarnessConfig(
                memtrace_mcp_url=None,
                memtrace_api_token=None,
                trace_db_path=db_path,
                trace_root=tmp_path,
                claude_command="claude",
                codex_command="codex",
                antigravity_command="agy",
                antigravity_output_mode="auto",
                cli_timeout_seconds=900,
                telegram_bot_token="test_bot_token",
                telegram_allowed_chat_ids={88888},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            scope = ProjectScope(
                name="rt_proj",
                workspace_id="ws_rt",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            triage = ChatTriage([scope])
            appr_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
            psess_mgr = PrimarySessionManager(trace_store)
            gateway = TelegramGateway(config, appr_mgr, triage, psess_mgr, [scope])

            # Attacker attempting to send commands from unallowed chat ID 66666
            attacker_update = {
                "update_id": 1,
                "message": {"chat": {"id": 66666}, "text": "/approve appr_fake123"},
            }
            res = gateway.process_update(attacker_update)
            self.assertIsNone(res, "Attacker message was not dropped at transport layer!")

            # Attacker attempting to trigger arbitrary task execution from unallowed chat ID
            attacker_task_update = {
                "update_id": 2,
                "message": {"chat": {"id": 66666}, "text": "Execute malicious code in project rt_proj"},
            }
            res_task = gateway.process_update(attacker_task_update)
            self.assertIsNone(res_task, "Attacker task execution attempt was not dropped!")

    def test_red_team_acceptance_scope_and_boundary_rejection(self) -> None:
        """Red Team Verification 2: Scope & Boundary Rules Rejection"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope = ProjectScope(
                name="secure_project",
                workspace_id="ws_secure",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
                off_limits=["production database", "deploy to prod"],
            )
            triage = ChatTriage([scope])

            # Request to open PR (out of scope for Harness remote ops)
            res1 = triage.triage_message("Open a PR on GitHub for feature X")
            self.assertEqual(res1.kind, "out_of_scope")
            self.assertIn("超出 Harness 遠端操作的範圍", res1.rejection_message or "")

            # Request touching off-limits area
            res2 = triage.triage_message("Migrate the production database now", default_project="secure_project")
            self.assertEqual(res2.kind, "out_of_scope")
            self.assertIn("禁區規則", res2.rejection_message or "")

    def test_red_team_acceptance_approval_request_guarantees(self) -> None:
        """Red Team Verification 3: Approval Request Policy Guarantees"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "rt_appr.sqlite3"
            trace_store = TraceStore(db_path)
            appr_mgr = ApprovalManager(trace_store, {10001})

            req = appr_mgr.request_approval(
                conversation_id="conv_rt",
                workspace="ws_rt",
                working_directory=str(tmp_path),
                reason="git_push",
                proposed_action="git push origin release",
            )

            # Guarantee A: Request stays pending indefinitely until human explicit decision (no auto-timeout)
            data = trace_store.get_approval_request(req.id)
            self.assertIsNotNone(data)
            self.assertEqual(data["status"], "pending")

            # Guarantee B: Single terminal response rule (no replay or double-spend)
            ok1, msg1, req_data = appr_mgr.respond(req.id, "approve", chat_id=10001)
            self.assertTrue(ok1)
            self.assertIn("approved", msg1)

            ok2, msg2, _ = appr_mgr.respond(req.id, "reject", chat_id=10001)
            self.assertFalse(ok2)
            self.assertIn("已經是", msg2)

    def test_red_team_acceptance_llm_chat_triage(self) -> None:
        """Red Team Verification 4: LLM Chat Triage Integration"""
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "rt_triage.sqlite3"
            config = HarnessConfig(
                memtrace_mcp_url=None,
                memtrace_api_token=None,
                trace_db_path=db_path,
                trace_root=tmp_path,
                claude_command="claude",
                codex_command="codex",
                antigravity_command="agy",
                antigravity_output_mode="auto",
                cli_timeout_seconds=900,
                telegram_bot_token="test_bot_token",
                telegram_allowed_chat_ids={88888},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            scope1 = ProjectScope(
                name="proj_alpha",
                workspace_id="ws_alpha",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            scope2 = ProjectScope(
                name="proj_beta",
                workspace_id="ws_beta",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            triage = ChatTriage([scope1, scope2], config)

            fake_res = ProcessResult(
                command=["claude", "--print", "prompt"],
                return_code=0,
                stdout="proj_beta",
                stderr="",
                started_at="2026-08-05T00:00:00Z",
                completed_at="2026-08-05T00:00:00Z",
                duration_ms=10,
            )
            with patch("memtrace_harness.cli_process.CliProcessRunner.run", return_value=fake_res):
                # There is no more "!" prefix or "task" kind — every plain message
                # routes to "chat" regardless of phrasing; project classification
                # for routing is unaffected.
                res = triage.triage_message("Help me fix the auth issue in the second system")
                self.assertEqual(res.kind, "chat")
                self.assertIsNotNone(res.project_scope)
                self.assertEqual(res.project_scope.name, "proj_beta")
