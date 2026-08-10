from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.trace_store import TraceStore


class PrimarySessionTests(TestCase):
    def test_primary_session_logging_and_consolidation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.create_node.return_value = "mem_draft_123"

            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            # 1. Record turns
            psess_mgr.record_turn(
                project="proj_a", speaker="user", turn_type="chat", content="Hello harness"
            )
            psess_mgr.record_turn(
                project="proj_a", speaker="chat_model", turn_type="decision", content="Agreed on architecture spec for design"
            )
            psess_mgr.record_turn(
                project="proj_a", speaker="work_session_report", turn_type="dev_report", content="Loop finished with PASS result"
            )

            # 2. Check rehydration context
            rehydrated = psess_mgr.get_rehydration_context("proj_a")
            self.assertIn("Agreed on architecture spec", rehydrated)
            self.assertIn("Loop finished with PASS", rehydrated)

            # 3. Consolidate back to MemTrace
            consolidated_ids = psess_mgr.consolidate_to_memtrace("proj_a", "ws_test")
            self.assertEqual(len(consolidated_ids), 2)  # decision and dev_report are substantive
            mock_client.create_node.assert_called_once()

    def test_consolidation_without_memtrace_client_does_not_discard_substantive_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_no_client.sqlite3"
            trace_store = TraceStore(db_path)

            # No memtrace_client configured — a legitimate, documented state (README:
            # MemTrace MCP is optional). Nothing can be written yet, so a substantive
            # turn must NOT be marked consolidated, or it would be silently lost forever
            # once MemTrace does become reachable.
            psess_mgr = PrimarySessionManager(trace_store, memtrace_client=None)
            psess_mgr.record_turn(
                project="proj_b", speaker="user", turn_type="chat", content="hi"
            )
            psess_mgr.record_turn(
                project="proj_b", speaker="user", turn_type="decision", content="!ship the fix"
            )

            written = psess_mgr.consolidate_to_memtrace("proj_b", "ws_test")
            self.assertEqual(written, [])

            session_id = psess_mgr.primary_session_id_for_project("proj_b")
            remaining = trace_store.get_unconsolidated_turns(session_id)
            # The chat turn can be marked consolidated (nothing worth keeping), but the
            # decision turn must still be pending so a later pass can actually write it.
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["turn_type"], "decision")

    def test_classify_chat_fn_can_promote_plain_chat_to_substantive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_classify.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.create_node.return_value = "mem_draft_456"
            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            psess_mgr.record_turn(project="proj_c", speaker="user", turn_type="chat", content="hi there")
            psess_mgr.record_turn(
                project="proj_c",
                speaker="user",
                turn_type="chat",
                content="let's always fall back to Gemini before Sonnet from now on",
            )

            # A model classifier judges the second (unmarked "chat") turn as worth
            # remembering even though nothing forced it via turn_type.
            written = psess_mgr.consolidate_to_memtrace(
                "proj_c", "ws_test", classify_chat_fn=lambda contents: [False, True]
            )
            self.assertEqual(len(written), 1)
            mock_client.create_node.assert_called_once()
            body = mock_client.create_node.call_args.kwargs["body"]
            self.assertIn("fall back to Gemini", body)

    def test_classify_chat_fn_mismatched_length_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_classify_mismatch.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            psess_mgr.record_turn(project="proj_d", speaker="user", turn_type="chat", content="hi")

            # A malformed/short classifier response must not be trusted enough to
            # promote anything — better to miss a note than misattribute a verdict.
            written = psess_mgr.consolidate_to_memtrace(
                "proj_d", "ws_test", classify_chat_fn=lambda contents: []
            )
            self.assertEqual(written, [])
            mock_client.create_node.assert_not_called()

    def test_consolidate_preferences_creates_single_profile_node_on_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_pref.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.search_nodes.return_value = []  # no existing profile node yet
            mock_client.create_node.return_value = "mem_profile_1"
            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            psess_mgr.record_turn(project="proj_e", speaker="user", turn_type="chat", content="hi")
            psess_mgr.record_turn(
                project="proj_e", speaker="user", turn_type="chat", content="回覆一律用繁體中文"
            )

            written = psess_mgr.consolidate_preferences(
                "proj_e", "ws_pref", classify_fn=lambda contents: [False, True]
            )

            self.assertEqual(len(written), 1)
            mock_client.create_node.assert_called_once()
            mock_client.update_node.assert_not_called()
            kwargs = mock_client.create_node.call_args.kwargs
            self.assertEqual(kwargs["content_type"], "preference")
            self.assertIn("繁體中文", kwargs["body"])

    def test_consolidate_preferences_merges_into_existing_profile_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_pref_merge.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.search_nodes.return_value = [
                {"id": "mem_profile_1", "title": "Operator preference profile"}
            ]
            mock_client.get_node.return_value = {"body": "- 既有偏好：早上比較忙"}
            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            psess_mgr.record_turn(
                project="proj_f", speaker="user", turn_type="chat", content="approve 太頻繁很煩"
            )

            written = psess_mgr.consolidate_preferences(
                "proj_f", "ws_pref", classify_fn=lambda contents: [True]
            )

            self.assertEqual(len(written), 1)
            mock_client.create_node.assert_not_called()
            mock_client.update_node.assert_called_once()
            kwargs = mock_client.update_node.call_args.kwargs
            self.assertIn("既有偏好：早上比較忙", kwargs["body"])
            self.assertIn("approve 太頻繁很煩", kwargs["body"])

    def test_consolidate_preferences_without_client_leaves_promoted_turns_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_pref_no_client.sqlite3"
            trace_store = TraceStore(db_path)
            psess_mgr = PrimarySessionManager(trace_store, memtrace_client=None)

            psess_mgr.record_turn(project="proj_g", speaker="user", turn_type="chat", content="hi")
            psess_mgr.record_turn(
                project="proj_g", speaker="user", turn_type="chat", content="請一律簡短回覆"
            )

            written = psess_mgr.consolidate_preferences(
                "proj_g", "ws_pref", classify_fn=lambda contents: [False, True]
            )
            self.assertEqual(written, [])

            session_id = psess_mgr.primary_session_id_for_project("proj_g")
            remaining = trace_store.get_unconsolidated_turns_for_preference(session_id)
            self.assertEqual(len(remaining), 1)
            self.assertIn("簡短回覆", remaining[0]["content"])

    def test_goal_and_preference_consolidation_do_not_consume_each_others_pending_turns(self) -> None:
        # The two axes must track pending/done independently — otherwise whichever pass
        # runs first would silently mark a turn "done" before the other axis ever saw it.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            db_path = tmp_path / "test_psess_two_axes.sqlite3"
            trace_store = TraceStore(db_path)
            mock_client = MagicMock()
            mock_client.search_nodes.return_value = []
            mock_client.create_node.return_value = "mem_x"
            psess_mgr = PrimarySessionManager(trace_store, mock_client)

            psess_mgr.record_turn(
                project="proj_h", speaker="user", turn_type="chat", content="請一律用繁體中文"
            )

            # Goal-oriented pass runs first and (correctly) does not consider this a
            # project decision — but it must not consume the turn for the other axis.
            goal_written = psess_mgr.consolidate_to_memtrace(
                "proj_h", "ws_goal", classify_chat_fn=lambda contents: [False]
            )
            self.assertEqual(goal_written, [])

            pref_written = psess_mgr.consolidate_preferences(
                "proj_h", "ws_pref", classify_fn=lambda contents: [True]
            )
            self.assertEqual(len(pref_written), 1)
