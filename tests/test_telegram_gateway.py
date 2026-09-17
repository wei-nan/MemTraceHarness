from __future__ import annotations

import json
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

    def test_model_deciding_to_start_a_task_locks_workspace_without_approval(self) -> None:
        # There is no more "!"/"/task" marker and no more front-door approval
        # request: a plain message goes to the model, and if the model itself
        # ends its reply with the HARNESS_TASK_START marker, the harness starts
        # the governed loop directly (in the background) and locks the workspace
        # — no approval request is created for it.
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

            # Don't actually spin up a real Agent Loop run in this unit test.
            run_new_task_mock = MagicMock(
                return_value=MagicMock(status="succeeded", recommendation="", conversation_id="x")
            )
            gateway._run_new_task = run_new_task_mock

            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            model_reply = ProcessResult(
                command=[],
                return_code=0,
                stdout=(
                    "好的，我來幫你把目前的設定檢查一遍並修好。\n"
                    "HARNESS_TASK_START::please review the current setup"
                ),
                stderr="",
                started_at="2026-09-10T00:00:00+00:00",
                completed_at="2026-09-10T00:00:01+00:00",
                duration_ms=500,
            )

            with patch("urllib.request.urlopen", return_value=mock_response), patch(
                "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
            ):
                update = {
                    "update_id": 200,
                    "message": {"chat": {"id": 12345}, "text": "please review the current setup"},
                }
                result = gateway.process_update(update)

            import threading as _threading

            for t in _threading.enumerate():
                if t.name.startswith("agent-loop-"):
                    t.join(timeout=5)

            # The marker line itself must not leak to the human.
            self.assertIsNotNone(result)
            self.assertNotIn("HARNESS_TASK_START", result)
            self.assertIn("設定檢查一遍", result)

            # The governed loop was actually started (synchronously, in this test)
            # with the goal taken from the marker line, and with no approval
            # request created for it — the model+human conversation itself is
            # the decision now.
            run_new_task_mock.assert_called_once()
            _scope_arg, _conv_id_arg, goal_arg = run_new_task_mock.call_args[0]
            self.assertEqual(goal_arg, "please review the current setup")
            with trace_store._connection() as conn:
                count = conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
            self.assertEqual(count, 0)
            self.assertIsNone(trace_store.get_workspace_lock("ws_test"))

    def test_ordinary_reply_without_marker_does_not_start_a_task(self) -> None:
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
            model_reply = ProcessResult(
                command=[],
                return_code=0,
                stdout="還在了解你的需求，可以再多說一點你想改哪個部分嗎？",
                stderr="",
                started_at="2026-09-10T00:00:00+00:00",
                completed_at="2026-09-10T00:00:01+00:00",
                duration_ms=500,
            )

            with patch("urllib.request.urlopen", return_value=mock_response), patch(
                "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
            ):
                update = {
                    "update_id": 201,
                    "message": {"chat": {"id": 12345}, "text": "我想改一下設定"},
                }
                gateway.process_update(update)

            self.assertIsNone(trace_store.get_workspace_lock("ws_test"))

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

    def _gateway_for_report_outcome_tests(self):
        tmp_dir = tempfile.TemporaryDirectory()
        tmp_path = Path(tmp_dir.name)
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
        self.addCleanup(tmp_dir.cleanup)
        return gateway, approval_mgr, trace_store

    def test_report_run_outcome_sends_approval_buttons_for_a_pending_checkpoint(self) -> None:
        # A run that stops "needs_human" because it hit a checkpoint (e.g. loop.py's
        # git-push gate) must surface the fresh ApprovalRequest with tappable
        # buttons — not a passive status line the human has to go find themselves.
        from unittest.mock import MagicMock

        gateway, approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_1",
            workspace="ws_test",
            working_directory="/tmp",
            reason="git_push",
            proposed_action="Execute git push for task: ...",
        )
        gateway.notify_approval_request = MagicMock()
        gateway.notify_all_allowlisted = MagicMock()
        gateway.send_message = MagicMock()

        summary = MagicMock(status="needs_human")
        gateway._report_run_outcome(conv_id="conv_1", summary=summary, final_text="passive status line")

        gateway.notify_approval_request.assert_called_once_with(req)
        gateway.notify_all_allowlisted.assert_not_called()
        gateway.send_message.assert_not_called()

    def test_report_run_outcome_falls_back_to_final_text_without_a_pending_approval(self) -> None:
        from unittest.mock import MagicMock

        gateway, _approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        gateway.notify_approval_request = MagicMock()
        gateway.notify_all_allowlisted = MagicMock()

        summary = MagicMock(status="needs_human")
        gateway._report_run_outcome(conv_id="conv_no_pending", summary=summary, final_text="ended without a checkpoint")

        gateway.notify_approval_request.assert_not_called()
        gateway.notify_all_allowlisted.assert_called_once_with("ended without a checkpoint")

    def test_report_run_outcome_uses_final_text_for_non_needs_human_status(self) -> None:
        from unittest.mock import MagicMock

        gateway, approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        # Even with a pending approval sitting around for some other reason, a
        # successful run's outcome must not be swallowed by it.
        approval_mgr.request_approval(
            conversation_id="conv_other",
            workspace="ws_test",
            working_directory="/tmp",
            reason="unattended_write",
            proposed_action="unrelated",
        )
        gateway.notify_approval_request = MagicMock()
        gateway.notify_all_allowlisted = MagicMock()

        summary = MagicMock(status="succeeded")
        gateway._report_run_outcome(conv_id="conv_succeeded", summary=summary, final_text="all done")

        gateway.notify_approval_request.assert_not_called()
        gateway.notify_all_allowlisted.assert_called_once_with("all done")

    def test_swipe_reply_to_pending_approval_still_clarifies_and_resumes(self) -> None:
        # A native swipe-reply to a pending approval's own message is the one
        # mechanical, non-model path left for touching a pending approval from
        # plain text — it must still resolve it as /clarify without needing any
        # model call at all.
        from unittest.mock import patch, MagicMock

        gateway, approval_mgr, trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_clarify",
            workspace="ws_test",
            working_directory="/tmp",
            reason="unattended_write",
            proposed_action="「範例任務」（mem_x）已定案可開發，是否核准開始？",
        )
        gateway._resume_approved_conversation = MagicMock()
        approval_mgr.record_telegram_message(req.id, chat_id=12345, message_id=901)

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "result": []}'
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response), patch(
            "memtrace_harness.cli_process.CliProcessRunner.run"
        ) as mock_run:
            update = {
                "update_id": 401,
                "message": {
                    "chat": {"id": 12345},
                    "text": "順便把 CHANGELOG 也更新一下",
                    "reply_to_message": {"message_id": 901},
                },
            }
            result = gateway.process_update(update)

        mock_run.assert_not_called()
        self.assertIn("已更新為", result)
        reloaded = approval_mgr.get_request(req.id)
        self.assertEqual(reloaded.status, "approved")
        gateway._resume_approved_conversation.assert_called_once()

    def test_unrelated_plain_text_does_not_touch_a_stale_pending_approval(self) -> None:
        # A plain message with no reply-to must never mechanically resolve some
        # unrelated pending approval sitting around — it's just chat, and the
        # only way to touch a pending approval from plain text now is a native
        # swipe-reply to its own message (or the explicit commands/buttons).
        from unittest.mock import patch, MagicMock
        from memtrace_harness.cli_process import ProcessResult

        gateway, approval_mgr, trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_stale",
            workspace="ws_test",
            working_directory="/tmp",
            reason="ambiguous_requirement",
            proposed_action="some stale unresolved request from hours ago",
        )
        gateway._resume_approved_conversation = MagicMock()

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "result": []}'
        mock_response.__enter__.return_value = mock_response
        model_reply = ProcessResult(
            command=[],
            return_code=0,
            stdout="目前沒有正在執行的任務喔。",
            stderr="",
            started_at="2026-09-04T00:00:00+00:00",
            completed_at="2026-09-04T00:00:01+00:00",
            duration_ms=50,
        )
        with patch("urllib.request.urlopen", return_value=mock_response), patch(
            "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
        ):
            update = {
                "update_id": 402,
                "message": {"chat": {"id": 12345}, "text": "目前任務狀態如何"},
            }
            gateway.process_update(update)

        reloaded = approval_mgr.get_request(req.id)
        self.assertEqual(reloaded.status, "pending")
        gateway._resume_approved_conversation.assert_not_called()

    def test_register_bot_commands_calls_set_my_commands(self) -> None:
        from unittest.mock import patch, MagicMock

        gateway, _approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "result": true}'
        mock_response.__enter__.return_value = mock_response

        with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
            ok = gateway.register_bot_commands()

        self.assertTrue(ok)
        called_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("setMyCommands", called_url)

    def test_ambiguous_requirement_approval_only_offers_an_abandon_button(self) -> None:
        gateway, approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_info",
            workspace="ws_test",
            working_directory="/tmp",
            reason="ambiguous_requirement",
            proposed_action="需要澄清範圍",
        )
        keyboard = gateway._approval_keyboard(req.id, req.reason)
        button_texts = [btn["text"] for row in keyboard for btn in row]
        self.assertEqual(button_texts, ["🛑 放棄這個任務"])

    def test_model_output_invalid_approval_offers_retry_and_give_up(self) -> None:
        gateway, approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_retry",
            workspace="ws_test",
            working_directory="/tmp",
            reason="model_output_invalid",
            proposed_action='{"action": "finish"}',
        )
        keyboard = gateway._approval_keyboard(req.id, req.reason)
        buttons = [(btn["text"], btn["callback_data"]) for row in keyboard for btn in row]
        self.assertEqual(
            buttons,
            [("🔁 重試", f"approve:{req.id}"), ("❌ 放棄", f"reject:{req.id}")],
        )

    def test_hydrate_github_issue_context_parses_stage_ref_and_calls_gh(self) -> None:
        from unittest.mock import patch, MagicMock
        from memtrace_harness.telegram_gateway import _hydrate_github_issue_context

        fake_issue = json.dumps(
            {
                "number": 112,
                "title": "被移除的協作者裝置端偵測不到自己已被移除",
                "body": "問題描述...",
                "url": "https://github.com/wei-nan/Beri/issues/112",
            }
        )
        fake_result = MagicMock(returncode=0, stdout=fake_issue, stderr="")
        with patch("memtrace_harness.telegram_gateway.subprocess.run", return_value=fake_result) as mock_run:
            item = _hydrate_github_issue_context(
                "gh:wei-nan/Beri#112", working_directory=Path("/tmp")
            )

        mock_run.assert_called_once()
        called_args = mock_run.call_args[0][0]
        self.assertEqual(called_args[:3], ["gh", "issue", "view"])
        self.assertIn("112", called_args)
        self.assertIn("wei-nan/Beri", called_args)
        self.assertIsNotNone(item)
        self.assertIn("112", item.title)
        self.assertIn("問題描述", item.body)

    def test_hydrate_github_issue_context_handles_gh_failure(self) -> None:
        from unittest.mock import patch, MagicMock
        from memtrace_harness.telegram_gateway import _hydrate_github_issue_context

        fake_result = MagicMock(returncode=1, stdout="", stderr="not found")
        with patch("memtrace_harness.telegram_gateway.subprocess.run", return_value=fake_result):
            item = _hydrate_github_issue_context(
                "gh:wei-nan/Beri#999", working_directory=Path("/tmp")
            )

        self.assertIsNone(item)
