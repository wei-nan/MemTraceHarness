from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.config import HarnessConfig
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore


class ScannerTests(TestCase):
    def test_scanner_backlog_and_locking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_scanner.sqlite3"
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
                telegram_bot_token="token",
                telegram_allowed_chat_ids={123},
                project_index_path=None,
                chat_provider="claude",
                chat_model="haiku",
                unattended_write_requires_approval=True,
            )
            scope = ProjectScope(
                name="proj_test",
                workspace_id="ws_scan",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            mock_memtrace = MagicMock()
            mock_memtrace.call_tool.return_value = {
                "nodes": [{"id": "mem_backlog_1"}, {"id": "mem_backlog_2"}]
            }
            approval_mgr = ApprovalManager(trace_store, {123})
            scanner = UnattendedScanner(
                config, trace_store, [scope], mock_memtrace, gateway=None, approval_mgr=approval_mgr
            )

            # First scan pass should find 2 backlog items and request approval for unattended run
            res = scanner.run_scan_pass()
            self.assertEqual(res.get("ws_scan"), "found_2_items")

            # Pending approval request should be created
            with trace_store._connection() as conn:
                row = conn.execute("SELECT id FROM approval_requests WHERE workspace = 'ws_scan' AND reason = 'unattended_write'").fetchone()
            self.assertIsNotNone(row)
            data = trace_store.get_approval_request(row[0])
            self.assertIsNotNone(data)
            self.assertEqual(data["status"], "pending")
            self.assertEqual(data["reason"], "unattended_write")

            # Lock remains held while waiting for approval
            lock = trace_store.get_workspace_lock("ws_scan")
            self.assertIsNotNone(lock)

            # Second scan pass should skip because lock is active
            res2 = scanner.run_scan_pass()
            self.assertEqual(res2.get("ws_scan"), "skipped_locked")
