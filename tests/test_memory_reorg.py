from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from memtrace_harness.memory_digest import _link_digest
from memtrace_harness.memory_reorg import (
    finish_rebuild,
    ground_review_ops,
    retire_covered_drafts,
    review_due,
    run_review_pass,
    start_rebuild,
    undo_reorg_run,
)
from memtrace_harness.memory_topics import finish_archived_days, render_topic_markdown, sync_topics_to_memtrace
from memtrace_harness.trace_store import TraceStore

TZ = ZoneInfo("Asia/Taipei")
NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
WS = "ws_mem"


def _entry(date: str, state: str, *turns: int, open_items: tuple[str, ...] = ()) -> dict:
    return {
        "date": date, "state": state, "turns": list(turns),
        "decisions": [{"text": f"{state}的決定", "turns": list(turns)}], "facts": [],
        "open_items": [{"text": t, "turns": list(turns)} for t in open_items],
    }


def _add_turn(store: TraceStore, content: str, created_at: str) -> int:
    turn_id = store.append_primary_session_turn(
        primary_session_id="psess_proj", project="proj", speaker="user", turn_type="chat", content=content
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (created_at, turn_id))
    return turn_id


class Base(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.client = MagicMock()
        self._node = 0

        def create(**kw):
            self._node += 1
            return f"mem_{self._node}"

        self.client.create_node.side_effect = create

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def topic(self, title: str, date: str, state: str = "現況") -> int:
        ids = self.store.save_topic_day(
            project="proj", workspace_id=WS, digest_date=date,
            topics=[{"topic_id": None, "title": title, "entry": _entry(date, state, 1)}], provider=None, model=None,
        )
        return ids[0]

    def synced(self) -> None:
        sync_topics_to_memtrace(self.store, self.client, "proj", WS)


class GroundReviewTests(Base):
    def _topics(self) -> list[dict]:
        a = self.topic("移動停利規則", "2026-10-01")
        b = self.topic("階梯式停利", "2026-10-02")
        c = self.topic("法人籌碼", "2026-10-03")
        d = self.topic("新的事", "2026-10-19")
        self.ids = (a, b, c, d)
        return self.store.list_memory_topics("proj", WS)

    def test_valid_ops_pass_and_each_matter_is_used_once(self) -> None:
        a, b, c, d = (self._topics() and self.ids)
        raw = {
            "merge": [{"keep": a, "absorb": [b], "title": "停利規則", "reason": "同一件事"}, {"keep": b, "absorb": [c]}],
            "resolve": [{"id": c, "reason": "已停用"}],
            "retitle": [{"id": d, "title": "新的事（更清楚）"}],
        }

        ops = ground_review_ops(raw, self.store.list_memory_topics("proj", WS), "2026-10-20")

        self.assertEqual([(o["keep"], o["absorb"], o["title"]) for o in ops["merge"]], [(a, [b], "停利規則")])
        self.assertEqual([o["id"] for o in ops["resolve"]], [c])
        self.assertEqual([o["id"] for o in ops["retitle"]], [d])

    def test_guards(self) -> None:
        a, b, c, d = (self._topics() and self.ids)
        topics = self.store.list_memory_topics("proj", WS)
        raw = {
            "merge": [{"keep": a, "absorb": [a]}, {"keep": a, "absorb": [999]}, {"keep": a, "absorb": []}],
            "resolve": [{"id": d, "reason": "剛談過的不能解決"}, {"id": b, "reason": ""}, {"id": 999, "reason": "x"}],
            "retitle": [{"id": a, "title": "階梯式停利"}, {"id": b, "title": ""}],  # a title that exists / empty
        }
        ops = ground_review_ops(raw, topics, "2026-10-20")
        self.assertEqual(ops, {"merge": [], "resolve": [], "retitle": []})
        self.assertEqual(ground_review_ops({"merge": "x", "resolve": 3}, topics, "2026-10-20"), {"merge": [], "resolve": [], "retitle": []})

    def test_the_pass_is_capped(self) -> None:
        for i in range(30):
            self.topic(f"事項{i}", "2026-09-01")
        topics = self.store.list_memory_topics("proj", WS)
        raw = {"resolve": [{"id": t["id"], "reason": "完成"} for t in topics]}
        self.assertEqual(len(ground_review_ops(raw, topics, "2026-10-20")["resolve"]), 10)


class ReviewPassTests(Base):
    def _model(self, answer: dict):
        return lambda prompt: (json.dumps(answer, ensure_ascii=False), "claude", "haiku")

    def test_merge_combines_entries_updates_the_kept_node_and_trashes_the_other(self) -> None:
        a = self.topic("移動停利規則", "2026-10-01", "固定停利")
        b = self.topic("階梯式停利", "2026-10-02", "改成階梯")
        for i in range(4):
            self.topic(f"其他{i}", "2026-10-03")
        self.synced()
        node_a = self.store.get_memory_topic(a)["node_id"]
        node_b = self.store.get_memory_topic(b)["node_id"]
        self.client.reset_mock()

        outcome = run_review_pass(
            self.store, self.client, "proj", WS,
            self._model({"merge": [{"keep": a, "absorb": [b], "title": "停利規則演進"}]}), now=NOW,
        )

        self.assertEqual(outcome.count, 1)
        kept = self.store.get_memory_topic(a)
        self.assertEqual(kept["title"], "停利規則演進")
        self.assertEqual([e["date"] for e in kept["entries"]], ["2026-10-01", "2026-10-02"])
        self.assertEqual(self.store.get_memory_topic(b)["status"], "merged")
        self.assertEqual(self.client.delete_node.call_args.kwargs["node_id"], node_b)
        self.assertEqual(self.client.update_node.call_args.kwargs["node_id"], node_a)
        self.assertEqual(self.client.update_node.call_args.kwargs["title"], "停利規則演進")
        self.client.create_node.assert_not_called()
        self.assertNotIn("階梯式停利", [t["title"] for t in self.store.list_memory_topics("proj", WS)])
        self.assertEqual(self.store.topic_nodes_for_date("proj", WS, "2026-10-02"), [node_a])  # the digest links to the kept node

    def test_undo_restores_everything(self) -> None:
        a = self.topic("移動停利規則", "2026-10-01")
        b = self.topic("階梯式停利", "2026-10-02")
        c = self.topic("法人籌碼", "2026-09-01")
        d = self.topic("其他0", "2026-10-03")
        for i in range(1, 3):
            self.topic(f"其他{i}", "2026-10-03")
        self.synced()
        node_b = self.store.get_memory_topic(b)["node_id"]
        outcome = run_review_pass(
            self.store, self.client, "proj", WS,
            self._model({"merge": [{"keep": a, "absorb": [b]}], "resolve": [{"id": c, "reason": "停用"}], "retitle": [{"id": d, "title": "別的名字"}]}),
            now=NOW,
        )
        self.assertEqual(outcome.count, 3)
        self.client.reset_mock()

        done, failed = undo_reorg_run(self.store, self.client, outcome.run_id)

        self.assertEqual((done, failed), (3, 0))
        self.assertEqual(self.client.restore_node.call_args.kwargs["node_id"], node_b)
        self.assertEqual(self.store.get_memory_topic(b)["status"], "active")
        back = self.store.get_memory_topic(a)
        self.assertEqual((back["title"], [e["date"] for e in back["entries"]]), ("移動停利規則", ["2026-10-01"]))
        self.assertEqual(self.store.get_memory_topic(c)["status"], "active")
        self.assertEqual(self.store.get_memory_topic(d)["title"], "其他0")
        self.assertEqual(self.store.topic_nodes_for_date("proj", WS, "2026-10-02"), [node_b])
        self.assertEqual(undo_reorg_run(self.store, self.client, outcome.run_id), (0, 0))  # only once

    def test_a_resolved_matter_is_marked_in_its_note_and_reopened_when_discussed_again(self) -> None:
        c = self.topic("法人籌碼", "2026-09-01")
        for i in range(5):
            self.topic(f"其他{i}", "2026-10-03")
        run_review_pass(self.store, self.client, "proj", WS, self._model({"resolve": [{"id": c, "reason": "改用別的資料源"}]}), now=NOW)
        resolved = self.store.get_memory_topic(c)
        self.assertEqual(resolved["status"], "resolved")
        self.assertIn("狀態：已解決", render_topic_markdown(resolved))
        self.assertIn(c, [t["id"] for t in self.store.list_memory_topics("proj", WS)])  # still searchable / matchable

        self.store.save_topic_day(
            project="proj", workspace_id=WS, digest_date="2026-10-19",
            topics=[{"topic_id": c, "title": "法人籌碼", "entry": _entry("2026-10-19", "又出問題", 5)}], provider=None, model=None,
        )

        self.assertEqual(self.store.get_memory_topic(c)["status"], "active")
        self.assertNotIn("已解決", render_topic_markdown(self.store.get_memory_topic(c)))

    def test_not_due_twice_in_a_week_and_not_with_too_few_matters(self) -> None:
        self.topic("a", "2026-10-01")
        self.assertFalse(review_due(self.store, "proj", WS, NOW))  # fewer than 6 matters
        for i in range(6):
            self.topic(f"m{i}", "2026-10-02")
        self.assertTrue(review_due(self.store, "proj", WS, NOW))
        run_review_pass(self.store, self.client, "proj", WS, self._model({}), now=NOW)
        wall_clock = datetime.now(timezone.utc)  # the run is stamped with the real time
        self.assertFalse(review_due(self.store, "proj", WS, wall_clock))
        self.assertTrue(review_due(self.store, "proj", WS, wall_clock + timedelta(days=8)))

    def test_a_model_failure_records_no_run(self) -> None:
        for i in range(6):
            self.topic(f"m{i}", "2026-10-02")

        def broken(prompt: str):
            raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            run_review_pass(self.store, self.client, "proj", WS, broken, now=NOW)
        self.assertTrue(review_due(self.store, "proj", WS, NOW))

    def test_plan_only_changes_nothing(self) -> None:
        a = self.topic("a", "2026-10-01")
        b = self.topic("b", "2026-10-02")
        outcome = run_review_pass(
            self.store, self.client, "proj", WS, self._model({"merge": [{"keep": a, "absorb": [b]}]}), now=NOW, force=True, plan_only=True
        )
        self.assertEqual(outcome.count, 1)
        self.assertEqual(self.store.get_memory_topic(b)["status"], "active")
        self.client.delete_node.assert_not_called()


class DraftRetirementTests(Base):
    def test_only_drafts_whose_every_day_is_archived_are_retired_and_undo_restores_them(self) -> None:
        t1 = _add_turn(self.store, "九月", "2026-09-30T01:00:00+00:00")
        t2 = _add_turn(self.store, "十月初", "2026-10-01T01:00:00+00:00")
        t3 = _add_turn(self.store, "十月二", "2026-10-02T01:00:00+00:00")
        seq = {t["id"]: t["turn_seq"] for t in self.store.get_primary_session_turns("psess_proj")}
        self.store.record_memory_draft(project="proj", workspace_id=WS, node_id="d_done", first_turn_seq=seq[t1], last_turn_seq=seq[t2])
        self.store.record_memory_draft(project="proj", workspace_id=WS, node_id="d_open", first_turn_seq=seq[t2], last_turn_seq=seq[t3])
        for day in ("2026-09-30", "2026-10-01"):
            self.store.save_topic_day(project="proj", workspace_id=WS, digest_date=day, topics=[], provider=None, model=None)
        finish_archived_days(self.store, "proj", WS, TZ)

        outcome = retire_covered_drafts(self.store, self.client, "proj", WS, TZ)

        self.assertEqual(outcome.count, 1)
        self.assertEqual(self.client.delete_node.call_args.kwargs["node_id"], "d_done")
        self.assertEqual([d["node_id"] for d in self.store.list_unretired_drafts("proj", WS)], ["d_open"])
        self.assertEqual([d["node_id"] for d in self.store.list_memory_drafts("proj", WS)], ["d_open"])  # no longer linked
        self.assertIsNone(retire_covered_drafts(self.store, self.client, "proj", WS, TZ))  # nothing more is covered

        undo_reorg_run(self.store, self.client, outcome.run_id)

        self.assertEqual(self.client.restore_node.call_args.kwargs["node_id"], "d_done")
        self.assertEqual(len(self.store.list_unretired_drafts("proj", WS)), 2)

    def test_a_failed_delete_is_not_marked_retired(self) -> None:
        t1 = _add_turn(self.store, "九月", "2026-09-30T01:00:00+00:00")
        seq = self.store.get_primary_session_turns("psess_proj")[0]["turn_seq"]
        self.store.record_memory_draft(project="proj", workspace_id=WS, node_id="d1", first_turn_seq=seq, last_turn_seq=seq)
        self.store.save_topic_day(project="proj", workspace_id=WS, digest_date="2026-09-30", topics=[], provider=None, model=None)
        finish_archived_days(self.store, "proj", WS, TZ)
        self.client.delete_node.side_effect = RuntimeError("down")

        outcome = retire_covered_drafts(self.store, self.client, "proj", WS, TZ)

        self.assertEqual((outcome.count, outcome.failed), (0, 1))
        self.assertEqual(len(self.store.list_unretired_drafts("proj", WS)), 1)


class RebuildTests(Base):
    def test_rebuild_supersedes_then_trashes_old_nodes_only_when_finished_and_can_be_undone(self) -> None:
        old = self.topic("舊分法的事項", "2026-10-01")
        self.synced()
        old_node = self.store.get_memory_topic(old)["node_id"]
        self.store.save_topic_day(project="proj", workspace_id=WS, digest_date="2026-10-01", topics=[], provider=None, model=None)

        run_id, resumed = start_rebuild(self.store, "proj", WS)

        self.assertFalse(resumed)
        self.assertEqual(self.store.topic_run_dates("proj"), set())  # every day is due again
        self.assertEqual(self.store.list_memory_topics("proj", WS), [])
        self.assertEqual(start_rebuild(self.store, "proj", WS), (run_id, True))  # an interrupted rebuild resumes
        self.client.delete_node.assert_not_called()  # nothing trashed before the new set exists

        new = self.topic("新分法的事項", "2026-10-01")
        self.synced()
        outcome = finish_rebuild(self.store, self.client, "proj", WS, run_id)

        self.assertEqual(outcome.count, 1)
        self.assertEqual(self.client.delete_node.call_args.kwargs["node_id"], old_node)
        self.assertEqual([t["title"] for t in self.store.list_memory_topics("proj", WS)], ["新分法的事項"])
        self.assertIsNone(self.store.running_reorg_run("proj", WS, "rebuild"))

        done, failed = undo_reorg_run(self.store, self.client, run_id)

        self.assertEqual(failed, 0)
        self.assertEqual(self.client.restore_node.call_args.kwargs["node_id"], old_node)
        self.assertEqual([t["title"] for t in self.store.list_memory_topics("proj", WS)], ["舊分法的事項"])
        self.assertEqual(self.store.get_memory_topic(new)["status"], "superseded")

    def test_undo_of_a_node_gone_from_the_trash_recreates_it_from_the_local_row(self) -> None:
        a = self.topic("a", "2026-10-01")
        b = self.topic("b", "2026-10-02")
        for i in range(4):
            self.topic(f"m{i}", "2026-10-03")
        self.synced()
        outcome = run_review_pass(
            self.store, self.client, "proj", WS, lambda p: (json.dumps({"merge": [{"keep": a, "absorb": [b]}]}), None, None), now=NOW
        )
        self.client.restore_node.side_effect = RuntimeError("expired from the trash")
        self.client.reset_mock(side_effect=False)
        self.client.restore_node.side_effect = RuntimeError("expired from the trash")
        self.client.create_node.side_effect = ["mem_recreated"]

        undo_reorg_run(self.store, self.client, outcome.run_id)

        self.assertEqual(self.store.get_memory_topic(b)["node_id"], "mem_recreated")


class ChatControlTests(Base):
    """Telling the Harness to reorganise or rebuild its memory, by command or in chat."""

    def _gateway(self):
        from memtrace_harness.approval import ApprovalManager
        from memtrace_harness.chat_triage import ChatTriage
        from memtrace_harness.config import HarnessConfig
        from memtrace_harness.primary_session import PrimarySessionManager
        from memtrace_harness.scope import ProjectScope
        from memtrace_harness.service import HarnessService

        path = Path(self._tmp.name)
        config = HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=path / "t.sqlite3", trace_root=path,
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900, chat_provider="claude",
            chat_model="haiku", unattended_write_requires_approval=True,
        )
        scope = ProjectScope(name="proj", workspace_id="ws_spec", working_directory=path, scope_file_path=path / "harness-scope.md")
        gw = HarnessService(config, ApprovalManager(self.store), ChatTriage([scope]), PrimarySessionManager(self.store), [scope])
        gw.send_message = MagicMock()
        gw.send_message_with_keyboard = MagicMock(return_value=3)
        gw.clear_message_keyboard = MagicMock()
        return gw, scope

    def test_the_rebuild_command_only_asks_and_a_tap_starts_it(self) -> None:
        from tests.support import relay, tap

        _add_turn(self.store, "第一天", "2026-10-01T01:00:00+00:00")
        _add_turn(self.store, "第二天", "2026-10-02T01:00:00+00:00")
        gw, scope = self._gateway()
        gw.memory_rebuild_now = MagicMock()

        relay(gw, {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/rebuild"}})

        gw.memory_rebuild_now.assert_not_called()  # asking never starts it
        text, keyboard = gw.send_message_with_keyboard.call_args.args[1:3]
        self.assertIn("2 天", text)
        self.assertIn("舊的事項節點才會移到垃圾桶", text)
        self.assertEqual([b["id"] for b in keyboard[0]], ["mem_rebuild_ok:proj", "mem_rebuild_no:proj"])

        tap(gw, {"id": "cb", "data": "mem_rebuild_no:proj", "message": {"chat": {"id": 7}, "message_id": 4}})
        gw.memory_rebuild_now.assert_not_called()
        self.assertIn("沒有重建", gw.send_message.call_args.args[1])

        tap(gw, {"id": "cb", "data": "mem_rebuild_ok:proj", "message": {"chat": {"id": 7}, "message_id": 4}})
        gw.memory_rebuild_now.assert_called_once_with(scope, 7)

    def test_the_reorganize_command_starts_at_once(self) -> None:
        from tests.support import relay

        gw, scope = self._gateway()
        gw.memory_reorg_now = MagicMock()
        relay(gw, {"update_id": 1, "message": {"chat": {"id": 7}, "text": "/reorganize"}})
        gw.memory_reorg_now.assert_called_once_with(scope, 7)
        gw.memory_reorg_now = None
        relay(gw, {"update_id": 2, "message": {"chat": {"id": 7}, "text": "/reorganize"}})
        self.assertIn("沒有啟用", gw.send_message.call_args.args[1])

    def test_both_are_listed_as_commands(self) -> None:
        gw, _ = self._gateway()
        self.assertTrue({"rebuild", "reorganize"} <= {c["command"] for c in gw.commands()})

    def test_the_chat_model_is_told_about_both_markers_and_that_rebuild_is_confirmed_by_a_tap(self) -> None:
        gw, _ = self._gateway()
        self.assertEqual(gw._extract_marker_line("好\nHARNESS_MEMORY_REBUILD::重新分題", gw._MEMORY_REBUILD_MARKER), ("好", "重新分題"))
        self.assertEqual(gw._extract_marker_line("好\nHARNESS_MEMORY_REORG::合併", gw._MEMORY_REORG_MARKER), ("好", "合併"))

    def test_a_rebuild_run_reports_and_a_failure_says_how_to_resume(self) -> None:
        from unittest.mock import patch

        from memtrace_harness import duties
        from memtrace_harness.memory_reorg import ReorgOutcome

        gw, scope = self._gateway()
        gw.notify_reorg_outcome = MagicMock()
        with patch.object(duties, "rebuild_matters", return_value=ReorgOutcome("rebuild", "proj", 5, ["重建完成"], 3)):
            duties.run_memory_rebuild(MagicMock(), self.store, self.client, [scope], gw, only_project="proj", audience=7)
        gw.notify_reorg_outcome.assert_called_once()
        with patch.object(duties, "rebuild_matters", return_value=None):
            duties.run_memory_rebuild(MagicMock(), self.store, self.client, [scope], gw, only_project="proj", audience=7)
        self.assertIn("接著做", gw.send_message.call_args.args[1])
        with patch.object(duties, "rebuild_matters", side_effect=RuntimeError("model down")):
            duties.run_memory_rebuild(MagicMock(), self.store, self.client, [scope], gw, only_project="proj", audience=7)
        self.assertIn("model down", gw.send_message.call_args.args[1])
        self.assertIn("接著做", gw.send_message.call_args.args[1])

    def test_a_second_rebuild_while_one_runs_is_refused(self) -> None:
        from memtrace_harness import duties

        _, scope = self._gateway()
        duties._rebuilding.add("proj")
        try:
            logged: list[str] = []
            config = MagicMock()
            config.schedule_timezone = "Asia/Taipei"
            self.assertIsNone(duties.rebuild_matters(config, self.store, self.client, scope, log=logged.append))
            self.assertIn("already running", logged[0])
        finally:
            duties._rebuilding.discard("proj")
