from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


class TelegramGatewayTests(TestCase):
    def test_telegram_gateway_allowlist_filtering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test.sqlite3"
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
                telegram_bot_token="fake_bot_token",
                telegram_allowed_chat_ids={12345},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
            scope = ProjectScope(
                name="test_proj",
                workspace_id="ws_test",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            triage = ChatTriage([scope])
            psess_mgr = PrimarySessionManager(trace_store)
            gateway = TelegramGateway(config, approval_mgr, triage, psess_mgr, [scope])

            # Update from unallowed Chat ID (99999) must be dropped at transport layer
            unallowed_update = {
                "update_id": 100,
                "message": {"chat": {"id": 99999}, "text": "Run something"},
            }
            result = gateway.process_update(unallowed_update)
            self.assertIsNone(result)

            # Update from allowed Chat ID (12345) must be processed with mocked urlopen
            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response

            with patch("urllib.request.urlopen", return_value=mock_response):
                allowed_update = {
                    "update_id": 101,
                    "message": {"chat": {"id": 12345}, "text": "status"},
                }
                result_allowed = gateway.process_update(allowed_update)
                self.assertIsNotNone(result_allowed)
                self.assertIn("Harness Gateway 已上線", result_allowed)

    def test_chat_task_creates_approval_request_and_locks_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test.sqlite3"
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
                telegram_bot_token="fake_bot_token",
                telegram_allowed_chat_ids={12345},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
            scope = ProjectScope(
                name="test_proj",
                workspace_id="ws_test",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            triage = ChatTriage([scope])
            psess_mgr = PrimarySessionManager(trace_store)
            gateway = TelegramGateway(config, approval_mgr, triage, psess_mgr, [scope])

            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response

            with patch("urllib.request.urlopen", return_value=mock_response):
                update = {
                    "update_id": 200,
                    "message": {"chat": {"id": 12345}, "text": "!please review the current setup"},
                }
                result = gateway.process_update(update)

            # A pending approval must be created instead of running the loop directly,
            # and it must not simply be an empty acknowledgement.
            self.assertIsNotNone(result)
            self.assertIn("核准請求", result)
            self.assertIn("please review the current setup", result)

            lock = trace_store.get_workspace_lock("ws_test")
            self.assertIsNotNone(lock)

            # A second task for the same workspace must be rejected while locked,
            # not silently start a second concurrent run.
            with patch("urllib.request.urlopen", return_value=mock_response):
                update2 = {
                    "update_id": 201,
                    "message": {"chat": {"id": 12345}, "text": "!please also review the tests"},
                }
                result2 = gateway.process_update(update2)
            self.assertIsNotNone(result2)
            self.assertIn("目前有任務正在執行中", result2)

    def test_plain_chat_message_gets_a_quick_reply_without_approval_or_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test.sqlite3"
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
                telegram_bot_token="fake_bot_token",
                telegram_allowed_chat_ids={12345},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
            scope = ProjectScope(
                name="test_proj",
                workspace_id="ws_test",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            triage = ChatTriage([scope])
            psess_mgr = PrimarySessionManager(trace_store)
            gateway = TelegramGateway(config, approval_mgr, triage, psess_mgr, [scope])

            from unittest.mock import patch, MagicMock
            from memtrace_harness.cli_process import ProcessResult

            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            fake_cli_result = ProcessResult(
                command=[],
                return_code=0,
                stdout="哈囉！有什麼我能幫忙的嗎？",
                stderr="",
                started_at="2026-08-07T00:00:00+00:00",
                completed_at="2026-08-07T00:00:01+00:00",
                duration_ms=500,
            )

            with patch("urllib.request.urlopen", return_value=mock_response), patch(
                "memtrace_harness.cli_process.CliProcessRunner.run", return_value=fake_cli_result
            ):
                update = {
                    "update_id": 300,
                    "message": {"chat": {"id": 12345}, "text": "你好"},
                }
                result = gateway.process_update(update)

            # A plain message (no "!") must reply directly — no approval request,
            # no workspace lock, no governed loop — or "just chatting" would always
            # queue a write task the way the old unconditional "task" routing did.
            self.assertIn("哈囉！有什麼我能幫忙的嗎？", result)
            self.assertIn("claude/haiku", result)
            self.assertIsNone(trace_store.get_workspace_lock("ws_test"))
            with trace_store._connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
            self.assertEqual(count, 0)
