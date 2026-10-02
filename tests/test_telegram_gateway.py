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

    def test_chat_reply_preallowlists_memtrace_read_tools_for_claude_only(self) -> None:
        from unittest.mock import MagicMock, patch
        from memtrace_harness.cli_process import ProcessResult

        gateway, _mgr, _store = self._gateway_for_report_outcome_tests()
        gateway.send_message = MagicMock()
        scope = gateway.projects[0]
        reply = ProcessResult(
            command=[], return_code=0, stdout="好", stderr="",
            started_at="2026-09-30T00:00:00+00:00", completed_at="2026-09-30T00:00:01+00:00",
            duration_ms=1,
        )
        with patch(
            "memtrace_harness.cli_process.CliProcessRunner.run", return_value=reply
        ) as run:
            gateway._chat_reply_and_maybe_start_task(scope, 12345, "查一下知識庫")
        cmd = run.call_args.args[0]
        allowed = cmd[cmd.index("--allowedTools") + 1]
        self.assertIn("mcp__memtrace__search_nodes", allowed)
        self.assertNotIn("update_node", allowed)
        self.assertNotIn("create_node", allowed)
        # --allowedTools is variadic: it must come before the flag that precedes the prompt.
        self.assertLess(cmd.index("--allowedTools"), cmd.index("--print"))
        self.assertIn("不需要先徵求任何核准", cmd[-1])

    def test_expire_stale_approvals_closes_old_pending_only(self) -> None:
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock

        gateway, mgr, store = self._gateway_for_report_outcome_tests()
        gateway.notify_all_allowlisted = MagicMock()
        gateway.clear_message_keyboard = MagicMock()
        old = mgr.request_approval(
            conversation_id="conv_old", workspace="ws_test", working_directory="/tmp",
            reason="ambiguous_requirement", proposed_action="old",
        )
        mgr.record_telegram_message(old.id, chat_id=12345, message_id=7)
        fresh = mgr.request_approval(
            conversation_id="conv_new", workspace="ws_test", working_directory="/tmp",
            reason="git_push", proposed_action="fresh",
        )
        other = mgr.request_approval(
            conversation_id="conv_other", workspace="ws_elsewhere", working_directory="/tmp",
            reason="git_push", proposed_action="other project",
        )
        # 13h later: `old` and `fresh` are both stale by then unless created "later";
        # age only `old` by evaluating just past its TTL.
        later = datetime.now(timezone.utc) + timedelta(hours=13)
        with store._connection() as conn:
            conn.execute(
                "UPDATE approval_requests SET created_at = ? WHERE id = ?",
                ((later - timedelta(hours=1)).isoformat(), fresh.id),
            )
        expired = gateway.expire_stale_approvals(12, now=later)

        self.assertEqual(expired, [old.id])
        self.assertEqual(mgr.get_request(old.id).status, "expired")
        self.assertEqual(mgr.get_request(fresh.id).status, "pending")
        self.assertEqual(mgr.get_request(other.id).status, "pending")  # other workspace untouched
        gateway.clear_message_keyboard.assert_called_once_with(12345, 7)
        gateway.notify_all_allowlisted.assert_called_once()
        self.assertEqual(gateway.expire_stale_approvals(0, now=later), [])  # ttl 0 disables

    def test_config_change_required_approval_only_offers_give_up(self) -> None:
        gateway, approval_mgr, _trace_store = self._gateway_for_report_outcome_tests()
        req = approval_mgr.request_approval(
            conversation_id="conv_cfg",
            workspace="ws_test",
            working_directory="/tmp",
            reason="config_change_required",
            proposed_action="agent_loop 已停用",
        )
        keyboard = gateway._approval_keyboard(req.id, req.reason)
        buttons = [(btn["text"], btn["callback_data"]) for row in keyboard for btn in row]
        self.assertEqual(buttons, [("🛑 放棄這個任務", f"reject:{req.id}")])

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


def _build_schedule_gateway(tmp_path: Path):
    """Shared boilerplate for the schedule tests below: a real TraceStore-backed
    TelegramGateway with one project, same construction shape every other test in
    this file repeats inline — factored out here since every schedule test needs it."""
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
    return gateway, trace_store, scope


class ScheduleControlMarkerTests(TestCase):
    def _chat(self, gateway, reply: str, text: str) -> str:
        from unittest.mock import patch, MagicMock
        from memtrace_harness.cli_process import ProcessResult

        mock_response = MagicMock()
        mock_response.read.return_value = b'{"ok": true, "result": []}'
        mock_response.__enter__.return_value = mock_response
        model_reply = ProcessResult(
            command=[], return_code=0, stdout=reply, stderr="",
            started_at="2026-10-02T00:00:00+00:00", completed_at="2026-10-02T00:00:01+00:00",
            duration_ms=500,
        )
        with patch("urllib.request.urlopen", return_value=mock_response), patch(
            "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
        ):
            return gateway.process_update(
                {"update_id": 400, "message": {"chat": {"id": 12345}, "text": text}}
            )

    def _make(self, trace_store, scope) -> str:
        from datetime import datetime, timedelta, timezone

        return trace_store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="check prices",
            kind="interval", interval_seconds=600, chat_id=12345,
            next_run_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )

    def test_pause_marker_really_stops_the_schedule_firing(self) -> None:
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, trace_store, scope = _build_schedule_gateway(Path(tmp_dir))
            sid = self._make(trace_store, scope)
            self.assertEqual(len(trace_store.list_due_schedules(datetime.now(timezone.utc))), 1)

            result = self._chat(
                gateway, f"好的，已暫停。\nHARNESS_SCHEDULE_PAUSE::{sid}::indefinite", "先暫停排程，明天再繼續"
            )

            self.assertNotIn("HARNESS_SCHEDULE_PAUSE", result)
            self.assertEqual(trace_store.list_due_schedules(datetime.now(timezone.utc)), [])
            self.assertTrue(trace_store.get_schedule(sid)["active"])

    def test_resume_marker_makes_it_fire_again_after_pause(self) -> None:
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, trace_store, scope = _build_schedule_gateway(Path(tmp_dir))
            sid = self._make(trace_store, scope)
            self._chat(gateway, f"ok\nHARNESS_SCHEDULE_PAUSE::{sid}::indefinite", "暫停")
            self._chat(gateway, f"ok\nHARNESS_SCHEDULE_RESUME::{sid}", "繼續")

            row = trace_store.get_schedule(sid)
            self.assertIsNone(row["paused_until"])
            later = datetime.now(timezone.utc) + timedelta(seconds=700)
            self.assertEqual(len(trace_store.list_due_schedules(later)), 1)

    def test_cancel_marker_deactivates_and_unknown_id_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, trace_store, scope = _build_schedule_gateway(Path(tmp_dir))
            sid = self._make(trace_store, scope)

            self._chat(gateway, "ok\nHARNESS_SCHEDULE_CANCEL::sched_nope", "取消")
            self.assertTrue(trace_store.get_schedule(sid)["active"])

            self._chat(gateway, f"ok\nHARNESS_SCHEDULE_CANCEL::{sid}", "取消")
            self.assertEqual(trace_store.list_schedules(scope.name), [])


class ScheduleCommandTests(TestCase):
    def test_model_deciding_to_start_a_schedule_creates_a_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, trace_store, scope = _build_schedule_gateway(tmp_path)

            from unittest.mock import patch, MagicMock
            from memtrace_harness.cli_process import ProcessResult

            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            model_reply = ProcessResult(
                command=[],
                return_code=0,
                stdout=(
                    "好的，我幫你排好每天早上檢查一次。\n"
                    "HARNESS_SCHEDULE_START::daily::09:00::daily backlog review"
                ),
                stderr="",
                started_at="2026-09-17T00:00:00+00:00",
                completed_at="2026-09-17T00:00:01+00:00",
                duration_ms=500,
            )

            with patch("urllib.request.urlopen", return_value=mock_response), patch(
                "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
            ):
                update = {
                    "update_id": 300,
                    "message": {"chat": {"id": 12345}, "text": "每天早上幫我檢查一次 backlog"},
                }
                result = gateway.process_update(update)

            # The marker line itself must not leak to the human.
            self.assertIsNotNone(result)
            self.assertNotIn("HARNESS_SCHEDULE_START", result)
            self.assertIn("每天早上", result)

            rows = trace_store.list_schedules(scope.name)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "daily")
            self.assertEqual(rows[0]["time_of_day"], "09:00")
            self.assertEqual(rows[0]["goal"], "daily backlog review")
            self.assertEqual(rows[0]["chat_id"], 12345)

    def test_malformed_schedule_spec_reports_error_and_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, trace_store, scope = _build_schedule_gateway(tmp_path)

            from unittest.mock import patch, MagicMock
            from memtrace_harness.cli_process import ProcessResult

            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            model_reply = ProcessResult(
                command=[],
                return_code=0,
                stdout="好，幫你排。\nHARNESS_SCHEDULE_START::daily::not-a-time::check backlog",
                stderr="",
                started_at="2026-09-17T00:00:00+00:00",
                completed_at="2026-09-17T00:00:01+00:00",
                duration_ms=500,
            )

            with patch("urllib.request.urlopen", return_value=mock_response), patch(
                "memtrace_harness.cli_process.CliProcessRunner.run", return_value=model_reply
            ):
                update = {
                    "update_id": 301,
                    "message": {"chat": {"id": 12345}, "text": "每天幫我檢查"},
                }
                gateway.process_update(update)

            self.assertEqual(trace_store.list_schedules(scope.name), [])

    def test_schedules_command_lists_existing_schedules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, trace_store, scope = _build_schedule_gateway(tmp_path)
            from datetime import datetime, timezone
            trace_store.create_schedule(
                project=scope.name,
                workspace_id=scope.workspace_id,
                goal="daily backlog review",
                kind="daily",
                time_of_day="09:00",
                chat_id=12345,
                next_run_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )

            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            with patch("urllib.request.urlopen", return_value=mock_response):
                update = {
                    "update_id": 302,
                    "message": {"chat": {"id": 12345}, "text": "/schedules"},
                }
                result = gateway.process_update(update)

            self.assertIsNotNone(result)
            self.assertIn("daily backlog review", result)
            self.assertIn("sched_", result)

    def test_schedules_command_with_no_schedules_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, _trace_store, _scope = _build_schedule_gateway(tmp_path)

            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            with patch("urllib.request.urlopen", return_value=mock_response):
                update = {
                    "update_id": 303,
                    "message": {"chat": {"id": 12345}, "text": "/schedules"},
                }
                result = gateway.process_update(update)

            self.assertIn("沒有排程", result)

    def test_schedule_cancel_deactivates_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, trace_store, scope = _build_schedule_gateway(tmp_path)
            from datetime import datetime, timezone
            schedule_id = trace_store.create_schedule(
                project=scope.name,
                workspace_id=scope.workspace_id,
                goal="daily backlog review",
                kind="interval",
                interval_seconds=3600,
                chat_id=12345,
                next_run_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            )

            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            with patch("urllib.request.urlopen", return_value=mock_response):
                update = {
                    "update_id": 304,
                    "message": {"chat": {"id": 12345}, "text": f"/schedule_cancel {schedule_id}"},
                }
                result = gateway.process_update(update)

            self.assertIn("已取消", result)
            self.assertEqual(trace_store.list_schedules(scope.name), [])

    def test_schedule_cancel_unknown_id_reports_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, _trace_store, _scope = _build_schedule_gateway(tmp_path)

            from unittest.mock import patch, MagicMock
            mock_response = MagicMock()
            mock_response.read.return_value = b'{"ok": true, "result": []}'
            mock_response.__enter__.return_value = mock_response
            with patch("urllib.request.urlopen", return_value=mock_response):
                update = {
                    "update_id": 305,
                    "message": {"chat": {"id": 12345}, "text": "/schedule_cancel sched_doesnotexist"},
                }
                result = gateway.process_update(update)

            self.assertIn("找不到", result)

    def test_run_due_schedule_starts_the_governed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, trace_store, scope = _build_schedule_gateway(tmp_path)
            from datetime import datetime, timezone

            from unittest.mock import MagicMock
            run_new_task_mock = MagicMock(
                return_value=MagicMock(status="succeeded", recommendation="", conversation_id="x")
            )
            gateway._run_new_task = run_new_task_mock

            schedule_id = trace_store.create_schedule(
                project=scope.name,
                workspace_id=scope.workspace_id,
                goal="daily backlog review",
                kind="interval",
                interval_seconds=3600,
                chat_id=12345,
                next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            row = trace_store.get_schedule(schedule_id)

            from unittest.mock import patch
            with patch.object(gateway, "send_message"):
                gateway.run_due_schedule(row)

            import threading as _threading
            for t in _threading.enumerate():
                if t.name.startswith("agent-loop-"):
                    t.join(timeout=5)

            run_new_task_mock.assert_called_once()
            _scope_arg, _conv_id_arg, goal_arg = run_new_task_mock.call_args[0]
            self.assertEqual(goal_arg, "daily backlog review")

    def test_run_due_schedule_skips_unknown_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            gateway, _trace_store, _scope = _build_schedule_gateway(tmp_path)
            from unittest.mock import MagicMock
            gateway._run_new_task = MagicMock()

            row = {
                "id": "sched_ghost",
                "project": "no_such_project",
                "goal": "g",
                "chat_id": 12345,
            }
            gateway.run_due_schedule(row)  # must not raise
            gateway._run_new_task.assert_not_called()


class KnowledgeBaseLocationsTests(TestCase):
    def test_agent_loop_context_items_name_the_cold_memory_workspace(self) -> None:
        # 2026-10-01: only the chat identity context used to mention the cold-memory
        # workspace; Agent Loop tasks got project scope + prior discussion only, so
        # consolidated history was write-only for Controller/Planner/Developer.
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, _, scope = _build_schedule_gateway(Path(tmp_dir))
            gateway.config = replace(gateway.config, harness_memory_workspace_id="ws_memory")

            items = {item.ref: item for item in gateway._project_context_items(scope)}

            kb = items["harness:knowledge-bases:test_proj"]
            self.assertEqual(kb.content_type, "context")
            self.assertIn("`ws_test`", kb.body)
            self.assertIn("`ws_memory`", kb.body)
            self.assertIn("Draft: test_proj <YYYY-MM-DD HH:MM>", kb.body)
            self.assertIn("`extracted_from`", kb.body)
            # Chat and Agent Loop are told the same thing.
            self.assertIn(kb.body, gateway._identity_context(scope))

    def test_falls_back_to_spec_workspace_when_no_memory_workspace_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, _, scope = _build_schedule_gateway(Path(tmp_dir))

            body = gateway._knowledge_base_locations(scope)

            self.assertIn("no dedicated memory workspace is configured", body)
            self.assertIn("spec workspace `ws_test` itself", body)


class MemoryInjectionTests(TestCase):
    def test_only_adopted_preferences_and_recent_digests_reach_chat_and_agent_loop(self) -> None:
        from memtrace_harness.memory_digest import resolve_preference

        with tempfile.TemporaryDirectory() as tmp_dir:
            gateway, trace_store, scope = _build_schedule_gateway(Path(tmp_dir))
            adopted = trace_store.add_preference_candidate(
                project="test_proj", scope="global", category="language_format",
                text="回覆一律用繁體中文", evidence=[], explicit=True, source_digest_date="2026-09-30",
            )
            trace_store.add_preference_candidate(
                project="test_proj", scope="global", category="other",
                text="還沒確認的候選", evidence=[], explicit=False, source_digest_date="2026-09-30",
            )
            resolve_preference(trace_store, adopted, "adopt")
            trace_store.save_memory_digest(
                project="test_proj", digest_date="2026-09-30", provider="claude", model="haiku",
                turn_count=3, digest={"summary": "決定資料期間為三年", "decisions": [], "open_items": []},
            )

            identity = gateway._identity_context(scope)
            items = {item.ref: item for item in gateway._project_context_items(scope)}

            self.assertIn("回覆一律用繁體中文", identity)
            self.assertNotIn("還沒確認的候選", identity)
            self.assertIn("決定資料期間為三年", identity)
            self.assertIn("決定資料期間為三年", items["harness:daily-digests:test_proj"].body)
            self.assertEqual(items["harness:daily-digests:test_proj"].content_type, "context")


class RepliedToMessageTests(TestCase):
    """2026-10-02: a bot never receives its own messages, and external scripts that post
    with the same bot token (daily_watchlist.py) never touch the Harness's log, so the chat
    model did not know what a swipe-reply to one of those pushes was about."""

    PUSH = "🔔 8046 南電 開盤漲幅 3.2%，符合進場條件，建議入場"

    def _update(self, text: str, reply_to: dict | None) -> dict:
        message = {"chat": {"id": 12345}, "text": text}
        if reply_to is not None:
            message["reply_to_message"] = reply_to
        return {"update_id": 1, "message": message}

    def _chat(self, update: dict):
        from unittest.mock import MagicMock, patch
        from memtrace_harness.cli_process import ProcessResult

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        gateway, store, scope = _build_schedule_gateway(Path(self._tmp.name))
        gateway.send_message = MagicMock()
        gateway.send_chat_action = MagicMock()
        reply = ProcessResult(
            command=[], return_code=0, stdout="了解", stderr="",
            started_at="2026-10-02T00:00:00+00:00", completed_at="2026-10-02T00:00:01+00:00", duration_ms=1,
        )
        with patch("memtrace_harness.cli_process.CliProcessRunner.run", return_value=reply) as run:
            gateway.process_update(update)
        turns = store.get_primary_session_turns("psess_test_proj")
        return run.call_args.args[0][-1], turns

    def test_replied_to_push_reaches_the_prompt_and_the_transcript(self) -> None:
        prompt, turns = self._chat(self._update("這檔後來怎麼了", {"message_id": 99, "text": self.PUSH}))

        self.assertIn("被回覆的訊息", prompt)
        self.assertIn(self.PUSH, prompt)
        self.assertIn("使用者訊息：這檔後來怎麼了", prompt)
        user_turn = next(t for t in turns if t["speaker"] == "user")
        # What the human typed reads first; the quote follows so a later digest still
        # knows what the short reply referred to.
        self.assertTrue(user_turn["content"].startswith("這檔後來怎麼了\n（回覆的訊息：「🔔 8046"))

    def test_a_long_quote_is_truncated_in_the_stored_turn_but_not_the_prompt(self) -> None:
        long_push = "報價 " + "x" * 800
        prompt, turns = self._chat(self._update("看一下", {"message_id": 99, "text": long_push}))

        self.assertIn(long_push, prompt)
        stored = next(t for t in turns if t["speaker"] == "user")["content"]
        self.assertLess(len(stored), 300)
        self.assertTrue(stored.endswith("…」）"))

    def test_a_caption_counts_and_a_reply_without_text_is_ignored(self) -> None:
        prompt, _ = self._chat(self._update("這張圖呢", {"message_id": 99, "caption": "走勢圖說明"}))
        self.assertIn("走勢圖說明", prompt)

        prompt, turns = self._chat(self._update("貼圖那則", {"message_id": 99, "sticker": {}}))
        self.assertNotIn("被回覆的訊息", prompt)
        self.assertEqual(next(t for t in turns if t["speaker"] == "user")["content"], "貼圖那則")

    def test_a_plain_message_is_unchanged(self) -> None:
        prompt, turns = self._chat(self._update("你好", None))
        self.assertNotIn("被回覆的訊息", prompt)
        self.assertEqual(next(t for t in turns if t["speaker"] == "user")["content"], "你好")


class ScheduledRunLoggingTests(TestCase):
    def _gateway(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        gateway, store, scope = _build_schedule_gateway(Path(self._tmp.name))
        gateway.send_message = MagicMock()
        return gateway, store, scope

    def test_trigger_and_result_are_typed_and_tagged_with_the_schedule(self) -> None:
        from unittest.mock import MagicMock, patch

        gateway, store, scope = self._gateway()
        summary = MagicMock(conversation_id="chat_x", status="succeeded", recommendation="未達停損門檻")
        with patch("memtrace_harness.telegram_gateway.load_role_profiles", return_value={}), patch(
            "memtrace_harness.telegram_gateway.build_role_adapter_candidates", return_value={}
        ), patch("memtrace_harness.telegram_gateway.AgentLoopRunner") as runner:
            runner.return_value.run.return_value = summary
            gateway._run_new_task(scope, "chat_x", "追蹤持股", schedule_id="sched_a")
            gateway._run_new_task(scope, "chat_y", "請開發功能")

        turns = store.get_primary_session_turns("psess_test_proj")
        scheduled, human = turns
        self.assertEqual((scheduled["turn_type"], scheduled["schedule_id"]), ("schedule_report", "sched_a"))
        self.assertIn("排程 sched_a 執行完成", scheduled["content"])
        self.assertIn("未達停損門檻", scheduled["content"])
        self.assertEqual((human["turn_type"], human["schedule_id"]), ("dev_report", None))

    def test_due_schedule_logs_a_typed_trigger_and_passes_its_id_on(self) -> None:
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        gateway, store, scope = self._gateway()
        gateway._start_or_queue_task = MagicMock()
        schedule_id = store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="追蹤持股", kind="interval",
            interval_seconds=600, chat_id=12345, next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )

        gateway.run_due_schedule(store.get_schedule(schedule_id))

        [trigger] = store.get_primary_session_turns("psess_test_proj")
        self.assertEqual((trigger["turn_type"], trigger["schedule_id"]), ("schedule_trigger", schedule_id))
        gateway._start_or_queue_task.assert_called_once()
        self.assertEqual(gateway._start_or_queue_task.call_args.kwargs["schedule_id"], schedule_id)

    def test_a_crashed_scheduled_run_is_still_recorded(self) -> None:
        import threading
        from unittest.mock import MagicMock

        gateway, store, scope = self._gateway()
        gateway._run_new_task = MagicMock(side_effect=RuntimeError("boom"))

        gateway._start_or_queue_task(scope, "追蹤持股", 12345, schedule_id="sched_a")
        for t in threading.enumerate():
            if t.name.startswith("agent-loop-"):
                t.join(timeout=5)

        [report] = store.get_primary_session_turns("psess_test_proj")
        self.assertEqual((report["turn_type"], report["schedule_id"]), ("schedule_report", "sched_a"))
        self.assertIn("執行失敗", report["content"])


class PreferenceCorrectionInChatTests(TestCase):
    """The Harness adopts preferences itself, so the human has to be able to undo one in
    the very conversation where they notice it is wrong."""

    def _gateway(self):
        from unittest.mock import MagicMock

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        gateway, store, scope = _build_schedule_gateway(Path(self._tmp.name))
        gateway.send_message = MagicMock()
        gateway.send_chat_action = MagicMock()
        self.rule = store.add_preference_candidate(
            project="test_proj", scope="global", category="communication", text="回覆簡短", evidence=[],
            explicit=True, source_digest_date="2026-10-01", status="adopted", adopted_by="harness-digest",
        )
        return gateway, store, scope

    def _chat(self, gateway, scope, model_output: str, text: str = "那個不用了"):
        from unittest.mock import patch
        from memtrace_harness.cli_process import ProcessResult

        reply = ProcessResult(
            command=[], return_code=0, stdout=model_output, stderr="",
            started_at="2026-10-02T00:00:00+00:00", completed_at="2026-10-02T00:00:01+00:00", duration_ms=1,
        )
        with patch("memtrace_harness.cli_process.CliProcessRunner.run", return_value=reply) as run:
            sent = gateway._chat_reply_and_maybe_start_task(scope, 12345, text)
        return sent, run.call_args.args[0][-1]

    def test_prompt_lists_the_rules_with_ids_and_the_correction_marker(self) -> None:
        gateway, _store, scope = self._gateway()

        _, prompt = self._chat(gateway, scope, "好")

        self.assertIn(f"[#{self.rule}] 回覆簡短", prompt)
        self.assertIn("HARNESS_PREFERENCE_CORRECT::<編號>::retire", prompt)
        self.assertIn("any of them may be wrong", prompt)

    def test_no_marker_instruction_when_there_are_no_adopted_rules(self) -> None:
        gateway, store, scope = self._gateway()
        store.update_preference_rule(self.rule, status="retired", expected_statuses=("adopted",), retire_reason="x")

        _, prompt = self._chat(gateway, scope, "好")

        self.assertNotIn("HARNESS_PREFERENCE_CORRECT", prompt)

    def test_a_correction_marker_retires_the_rule_and_is_not_shown(self) -> None:
        gateway, store, scope = self._gateway()

        sent, _ = self._chat(
            gateway, scope, f"好，之後不再限制長度。\nHARNESS_PREFERENCE_CORRECT::{self.rule}::retire"
        )

        self.assertEqual(store.get_preference_rule(self.rule)["status"], "retired")
        self.assertNotIn("HARNESS_PREFERENCE_CORRECT", sent)
        self.assertIn("好，之後不再限制長度。", sent)
        self.assertIn(f"🧠 已撤回偏好 #{self.rule}：回覆簡短", sent)
        stored = store.get_primary_session_turns("psess_test_proj")
        self.assertIn("已撤回偏好", [t for t in stored if t["speaker"] == "assistant"][0]["content"])

    def test_other_markers_still_work_when_a_correction_sits_above_them(self) -> None:
        from unittest.mock import MagicMock

        gateway, store, scope = self._gateway()
        gateway._start_or_queue_task = MagicMock()

        self._chat(
            gateway, scope,
            f"收到。\nHARNESS_PREFERENCE_CORRECT::{self.rule}::replace::回覆詳細\nHARNESS_TASK_START::跑回測",
        )

        gateway._start_or_queue_task.assert_called_once()
        self.assertEqual(gateway._start_or_queue_task.call_args.args[1], "跑回測")
        new = [r for r in store.list_preference_rules(statuses=("adopted",))]
        self.assertEqual([r["text"] for r in new], ["回覆詳細"])

    def test_a_bad_marker_is_dropped_and_the_reply_is_still_sent(self) -> None:
        gateway, store, scope = self._gateway()

        sent, _ = self._chat(gateway, scope, "好\nHARNESS_PREFERENCE_CORRECT::9999::retire\nHARNESS_PREFERENCE_CORRECT::oops")

        self.assertEqual(store.get_preference_rule(self.rule)["status"], "adopted")
        self.assertTrue(sent.startswith("好"))
        self.assertNotIn("HARNESS_PREFERENCE_CORRECT", sent)
