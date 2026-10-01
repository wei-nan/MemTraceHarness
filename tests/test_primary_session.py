from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

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


def _stamp(store: TraceStore, turn_id: int, created_at: str) -> None:
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (created_at, turn_id))


class ScheduleNoiseTests(TestCase):
    """2026-10-02: a 10-minute schedule made up 54% of one project's log, and at one
    real moment 8 of the 10 turns the chat model saw (and all 5 'earlier substantive'
    slots) were schedule firings instead of the conversation."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.mgr = PrimarySessionManager(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fire(self, schedule_id: str, goal: str, result: str, at: str) -> None:
        trigger = self.mgr.record_turn(
            project="p", speaker="system", turn_type="schedule_trigger",
            content=f"排程 {schedule_id} 觸發：{goal}", schedule_id=schedule_id,
        )
        report = self.mgr.record_turn(
            project="p", speaker="work_session_report", turn_type="schedule_report",
            content=f"排程 {schedule_id} 執行完成（狀態 succeeded）：{result}", schedule_id=schedule_id,
        )
        _stamp(self.store, trigger, at)
        _stamp(self.store, report, at)

    def test_schedule_firings_do_not_crowd_the_conversation_window(self) -> None:
        for i in range(3):
            self.mgr.record_turn(project="p", speaker="user", turn_type="chat", content=f"使用者說 {i}")
        for i in range(12):
            self._fire("sched_a", "追蹤持股", f"第 {i} 次結果", "2026-10-01T05:00:00+00:00")
        self.mgr.record_turn(project="p", speaker="assistant", turn_type="chat", content="助理回覆")

        text = self.mgr.get_rehydration_context(
            "p", tz=ZoneInfo("Asia/Taipei"), now=datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        )

        recent = text.split("Recent transcript:\n")[1].split("\n\nScheduled runs")[0]
        self.assertEqual(recent.count("[user]"), 3)
        self.assertIn("助理回覆", recent)
        self.assertNotIn("排程", recent)
        # ... but the human can still be told what the schedule last reported.
        self.assertIn("近 24 小時觸發 12 次", text)
        self.assertIn("最近一次結果（10-01 13:00）：排程 sched_a 執行完成（狀態 succeeded）：第 11 次結果", text)
        self.assertNotIn("第 10 次結果", text)

    def test_older_substantive_slots_ignore_schedule_turns(self) -> None:
        self.mgr.record_turn(project="p", speaker="user", turn_type="decision", content="真正的決策")
        for i in range(8):
            self._fire("sched_a", "g", f"r{i}", "2026-10-01T05:00:00+00:00")
        for i in range(10):
            self.mgr.record_turn(project="p", speaker="user", turn_type="chat", content=f"閒聊 {i}")

        text = self.mgr.get_rehydration_context("p", now=datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc))

        earlier = text.split("Recent transcript:")[0]
        self.assertIn("真正的決策", earlier)
        self.assertNotIn("排程", earlier)

    def test_each_schedule_keeps_only_its_latest_result_and_old_ones_drop_out(self) -> None:
        self._fire("sched_old", "舊排程", "昨天的", "2026-09-29T05:00:00+00:00")
        self._fire("sched_a", "追蹤 A", "A 早上", "2026-10-01T01:00:00+00:00")
        self._fire("sched_a", "追蹤 A", "A 最新", "2026-10-01T05:00:00+00:00")
        self._fire("sched_b", "追蹤 B", "B 最新", "2026-10-01T04:00:00+00:00")

        text = self.mgr.get_rehydration_context("p", now=datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc))

        self.assertIn("A 最新", text)
        self.assertNotIn("A 早上", text)
        self.assertIn("B 最新", text)
        self.assertNotIn("sched_old", text)
        self.assertLess(text.index("sched_a"), text.index("sched_b"))  # newest activity first

    def test_a_schedule_whose_runs_all_failed_says_it_has_no_result(self) -> None:
        trigger = self.mgr.record_turn(
            project="p", speaker="system", turn_type="schedule_trigger",
            content="排程 sched_a 觸發：追蹤 A", schedule_id="sched_a",
        )
        _stamp(self.store, trigger, "2026-10-01T05:00:00+00:00")

        text = self.mgr.get_rehydration_context("p", now=datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc))

        self.assertIn("sched_a「追蹤 A」，近 24 小時觸發 1 次；這段期間沒有執行結果", text)

    def test_hourly_archive_skips_schedule_turns_and_marks_them_done(self) -> None:
        client = MagicMock()
        mgr = PrimarySessionManager(self.store, client)
        mgr.record_turn(project="p", speaker="user", turn_type="decision", content="真正的決策")
        self._fire("sched_a", "追蹤 A", "不該進 MemTrace", "2026-10-01T05:00:00+00:00")

        written = mgr.consolidate_to_memtrace("p", "ws_mem", classify_chat_fn=lambda c: [True] * len(c))

        self.assertEqual(len(written), 1)
        body = client.create_node.call_args.kwargs["body"]
        self.assertIn("真正的決策", body)
        self.assertNotIn("不該進 MemTrace", body)
        self.assertEqual(self.store.get_unconsolidated_turns(mgr.primary_session_id_for_project("p")), [])

    def test_a_pass_with_only_schedule_turns_writes_nothing_and_clears_them(self) -> None:
        client = MagicMock()
        mgr = PrimarySessionManager(self.store, client)
        self._fire("sched_a", "g", "r", "2026-10-01T05:00:00+00:00")

        self.assertEqual(mgr.consolidate_to_memtrace("p", "ws_mem"), [])

        client.create_node.assert_not_called()
        self.assertEqual(self.store.get_unconsolidated_turns(mgr.primary_session_id_for_project("p")), [])


class LegacyScheduleRetagTests(TestCase):
    def test_old_shaped_rows_get_the_new_types_once(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "t.sqlite3"
            store = TraceStore(db_path)
            rows = [
                ("user", "chat", "你好"),
                ("system", "decision", "排程 sched_one 觸發：追蹤 A"),
                ("system", "decision", "排程 sched_two 觸發：追蹤 B"),   # refused by the lock
                ("work_session_report", "dev_report", "Chat-triggered loop chat_1 completed with status succeeded: A 的結果"),
                ("user", "decision", "請開發某功能"),                      # a human-started task
                ("work_session_report", "dev_report", "Chat-triggered loop chat_2 completed with status succeeded: 開發結果"),
            ]
            with sqlite3.connect(db_path) as conn:
                for seq, (speaker, turn_type, content) in enumerate(rows, start=1):
                    conn.execute(
                        "INSERT INTO primary_sessions_hot_log (primary_session_id, project, turn_seq, created_at, "
                        "speaker, turn_type, content, consolidated) VALUES ('psess_p', 'p', ?, '2026-09-22T05:00:00+00:00', ?, ?, ?, 1)",
                        (seq, speaker, turn_type, content),
                    )

            for _ in range(2):  # the second open must change nothing
                turns = TraceStore(db_path).get_primary_session_turns("psess_p")
                self.assertEqual(
                    [(t["turn_type"], t["schedule_id"]) for t in turns],
                    [
                        ("chat", None),
                        ("schedule_trigger", "sched_one"),
                        ("schedule_trigger", "sched_two"),
                        # two triggers, one report: only the first got the workspace lock
                        ("schedule_report", "sched_one"),
                        ("decision", None),
                        ("dev_report", None),
                    ],
                )
            self.assertEqual(turns[3]["content"], rows[3][2])
