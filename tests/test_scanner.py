from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.config import HarnessConfig
from memtrace_harness.scanner import UnattendedScanner, _is_ready_task_node
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore


class TaskNodeReadinessTests(TestCase):
    def test_ready_task_node_matches_all_criteria(self) -> None:
        node = {
            "tags": ["task", "status:open", "Beri"],
            "body": (
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}'
            ),
        }
        self.assertTrue(_is_ready_task_node(node))

    def test_fenced_json_body_still_parses(self) -> None:
        node = {
            "tags": ["task", "status:open"],
            "body": (
                "```json\n"
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}\n'
                "```"
            ),
        }
        self.assertTrue(_is_ready_task_node(node))

    def test_rejects_wrong_status_tag(self) -> None:
        node = {
            "tags": ["task", "status:done"],
            "body": (
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}'
            ),
        }
        self.assertFalse(_is_ready_task_node(node))

    def test_rejects_missing_gate_pass(self) -> None:
        node = {
            "tags": ["task", "status:open"],
            "body": '{"checkpoint": "build", "current_stage": "dev", "gate_state": {"latest_verdict": "REJECT"}}',
        }
        self.assertFalse(_is_ready_task_node(node))

    def test_rejects_wrong_checkpoint_stage(self) -> None:
        node = {
            "tags": ["task", "status:open"],
            "body": '{"checkpoint": "plan", "current_stage": "plan"}',
        }
        self.assertFalse(_is_ready_task_node(node))

    def test_rejects_malformed_body(self) -> None:
        node = {"tags": ["task", "status:open"], "body": "not json"}
        self.assertFalse(_is_ready_task_node(node))


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
            ready_task_body = (
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}'
            )
            mock_memtrace = MagicMock()
            mock_memtrace.search_nodes.return_value = [
                {
                    "id": "mem_backlog_1",
                    "tags": ["task", "status:open"],
                    "body": ready_task_body,
                },
                {
                    "id": "mem_backlog_2",
                    "tags": ["task", "status:open"],
                    "body": ready_task_body,
                },
                {
                    # not yet at build/dev — must be excluded
                    "id": "mem_not_ready",
                    "tags": ["task", "status:open"],
                    "body": '{"checkpoint": "plan", "current_stage": "plan"}',
                },
                {
                    # missing the status:open tag — must be excluded
                    "id": "mem_done",
                    "tags": ["task", "status:done"],
                    "body": ready_task_body,
                },
            ]
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

    def _scanner_config(self, db_path: Path, tmp_path: Path) -> HarnessConfig:
        return HarnessConfig(
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

    def test_run_scan_pass_proposes_only_the_oldest_ready_task_and_previews_the_rest(self) -> None:
        # Multiple ready Task Nodes must be discussed one at a time (oldest first),
        # never bundled into a single blind run.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_scanner.sqlite3"
            trace_store = TraceStore(db_path)
            config = self._scanner_config(db_path, tmp_path)
            scope = ProjectScope(
                name="proj_test",
                workspace_id="ws_scan",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            ready_body = (
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}'
            )
            mock_memtrace = MagicMock()
            mock_memtrace.search_nodes.return_value = [
                {
                    "id": "mem_newer",
                    "title": "新的任務",
                    "tags": ["task", "status:open"],
                    "body": ready_body,
                    "created_at": "2026-09-02T00:00:00Z",
                },
                {
                    "id": "mem_older",
                    "title": "舊的任務",
                    "tags": ["task", "status:open"],
                    "body": ready_body,
                    "created_at": "2026-09-01T00:00:00Z",
                },
            ]
            approval_mgr = ApprovalManager(trace_store, {123})
            scanner = UnattendedScanner(
                config, trace_store, [scope], mock_memtrace, gateway=None, approval_mgr=approval_mgr
            )

            res = scanner.run_scan_pass()
            self.assertEqual(res.get("ws_scan"), "found_2_items")

            with trace_store._connection() as conn:
                row = conn.execute(
                    "SELECT stage_ref, proposed_action FROM approval_requests WHERE workspace = 'ws_scan'"
                ).fetchone()
            self.assertIsNotNone(row)
            stage_ref, proposed_action = row
            # Older node (earlier created_at) is proposed first, not the one MemTrace
            # happened to return first.
            self.assertEqual(stage_ref, "mem_older")
            self.assertIn("舊的任務", proposed_action)
            self.assertIn("還有 1 項待處理", proposed_action)
            self.assertIn("新的任務", proposed_action)

    def test_run_scan_pass_skips_a_declined_task_and_offers_the_next_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_scanner.sqlite3"
            trace_store = TraceStore(db_path)
            config = self._scanner_config(db_path, tmp_path)
            scope = ProjectScope(
                name="proj_test",
                workspace_id="ws_scan",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
            )
            ready_body = (
                '{"checkpoint": "build", "current_stage": "dev", '
                '"gate_state": {"latest_verdict": "PASS"}}'
            )
            mock_memtrace = MagicMock()
            mock_memtrace.search_nodes.return_value = [
                {
                    "id": "mem_older",
                    "title": "舊的任務",
                    "tags": ["task", "status:open"],
                    "body": ready_body,
                    "created_at": "2026-09-01T00:00:00Z",
                },
                {
                    "id": "mem_newer",
                    "title": "新的任務",
                    "tags": ["task", "status:open"],
                    "body": ready_body,
                    "created_at": "2026-09-02T00:00:00Z",
                },
            ]
            approval_mgr = ApprovalManager(trace_store, {123})
            scanner = UnattendedScanner(
                config, trace_store, [scope], mock_memtrace, gateway=None, approval_mgr=approval_mgr
            )

            # Operator declines the first-proposed (oldest) task and the lock frees up.
            res = scanner.run_scan_pass()
            self.assertEqual(res.get("ws_scan"), "found_2_items")
            with trace_store._connection() as conn:
                first_id = conn.execute(
                    "SELECT id FROM approval_requests WHERE workspace = 'ws_scan'"
                ).fetchone()[0]
            self.assertTrue(approval_mgr.trace_store.resolve_approval_request(first_id, "rejected"))
            trace_store.release_workspace_lock("ws_scan")

            # Next scan pass must skip the declined node and propose the next one.
            res2 = scanner.run_scan_pass()
            self.assertEqual(res2.get("ws_scan"), "found_1_items")
            with trace_store._connection() as conn:
                stage_ref = conn.execute(
                    "SELECT stage_ref FROM approval_requests WHERE workspace = 'ws_scan' AND status = 'pending'"
                ).fetchone()[0]
            self.assertEqual(stage_ref, "mem_newer")

    def test_find_ready_github_issues_filters_assigned_and_sorts_oldest_first(self) -> None:
        # 2026-09-05: GitHub Issues replaced MemTrace Task Nodes as Beri's backlog
        # source. Readiness = open AND unassigned.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_scanner.sqlite3"
            trace_store = TraceStore(db_path)
            config = self._scanner_config(db_path, tmp_path)
            scope = ProjectScope(
                name="proj_gh",
                workspace_id="ws_gh",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
                github_repo="acme/widget",
            )
            fake_issues = json.dumps(
                [
                    {
                        "number": 5,
                        "title": "新的 issue",
                        "body": "body5",
                        "assignees": [],
                        "createdAt": "2026-09-02T00:00:00Z",
                        "url": "https://github.com/acme/widget/issues/5",
                    },
                    {
                        "number": 3,
                        "title": "已被認領的 issue",
                        "body": "body3",
                        "assignees": [{"login": "someone"}],
                        "createdAt": "2026-09-01T00:00:00Z",
                        "url": "https://github.com/acme/widget/issues/3",
                    },
                    {
                        "number": 1,
                        "title": "最舊的 issue",
                        "body": "body1",
                        "assignees": [],
                        "createdAt": "2026-08-30T00:00:00Z",
                        "url": "https://github.com/acme/widget/issues/1",
                    },
                ]
            )
            scanner = UnattendedScanner(config, trace_store, [scope], None, gateway=None, approval_mgr=None)
            fake_result = MagicMock(returncode=0, stdout=fake_issues, stderr="")
            with patch("memtrace_harness.scanner.subprocess.run", return_value=fake_result) as mock_run:
                candidates = scanner._find_ready_github_issues(scope)

            mock_run.assert_called_once()
            called_args = mock_run.call_args[0][0]
            self.assertEqual(called_args[:3], ["gh", "issue", "list"])
            self.assertIn("acme/widget", called_args)

            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0]["id"], "gh:acme/widget#1")
            self.assertEqual(candidates[0]["title"], "最舊的 issue")
            self.assertEqual(candidates[1]["id"], "gh:acme/widget#5")

    def test_run_scan_pass_uses_github_issues_when_github_repo_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_scanner.sqlite3"
            trace_store = TraceStore(db_path)
            config = self._scanner_config(db_path, tmp_path)
            scope = ProjectScope(
                name="proj_gh",
                workspace_id="ws_gh",
                working_directory=tmp_path,
                scope_file_path=tmp_path / "harness-scope.md",
                github_repo="acme/widget",
            )
            fake_issues = json.dumps(
                [
                    {
                        "number": 112,
                        "title": "一個待辦",
                        "body": "詳細內容",
                        "assignees": [],
                        "createdAt": "2026-09-02T00:00:00Z",
                        "url": "https://github.com/acme/widget/issues/112",
                    }
                ]
            )
            approval_mgr = ApprovalManager(trace_store, {123})
            scanner = UnattendedScanner(
                config, trace_store, [scope], None, gateway=None, approval_mgr=approval_mgr
            )
            fake_result = MagicMock(returncode=0, stdout=fake_issues, stderr="")
            with patch("memtrace_harness.scanner.subprocess.run", return_value=fake_result):
                res = scanner.run_scan_pass()

            self.assertEqual(res.get("ws_gh"), "found_1_items")
            with trace_store._connection() as conn:
                stage_ref = conn.execute(
                    "SELECT stage_ref FROM approval_requests WHERE workspace = 'ws_gh'"
                ).fetchone()[0]
            self.assertEqual(stage_ref, "gh:acme/widget#112")
