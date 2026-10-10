from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from memtrace_harness.memory_digest import DigestError, _link_digest
from memtrace_harness.memory_topics import (
    build_topics_prompt,
    clean_title,
    due_topic_dates,
    finish_archived_days,
    ground_topics,
    render_topic_markdown,
    run_topics_for_date,
    sync_topics_to_memtrace,
)
from memtrace_harness.trace_store import TraceStore

TZ = ZoneInfo("Asia/Taipei")
SESSION = "psess_proj"
DAY1, DAY2 = "2026-10-08", "2026-10-09"


def _add(store: TraceStore, speaker: str, content: str, created_at: str, turn_type: str = "chat") -> int:
    turn_id = store.append_primary_session_turn(
        primary_session_id=SESSION, project="proj", speaker=speaker, turn_type=turn_type, content=content
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (created_at, turn_id))
    return turn_id


def _model(*answers: dict):
    calls: list[str] = []
    queue = list(answers)

    def call(prompt: str):
        calls.append(prompt)
        return json.dumps(queue.pop(0), ensure_ascii=False), "claude", "haiku"

    return call, calls


def _item(text: str, *turns: int) -> dict:
    return {"text": text, "turns": list(turns)}


class GroundTopicsTests(TestCase):
    def test_keeps_only_items_citing_real_turns_and_drops_matters_without_any(self) -> None:
        raw = {
            "topics": [
                {
                    "match": None,
                    "title": "移動停利規則",
                    "state": "+8% 啟動，回撤 6×ATR% 出場",
                    "decisions": [_item("啟動點 +8%", 1, 2), _item("憑空的決定", 99)],
                    "facts": [_item("ATR 以 14 日計", 2)],
                    "open_items": [],
                },
                {"match": None, "title": "全是捏造", "state": "x", "decisions": [_item("沒有來源", 50)]},
                {"match": None, "title": "", "state": "沒有標題也沒有對象", "decisions": [_item("a", 1)]},
            ]
        }

        topics, dropped = ground_topics(raw, digest_date=DAY1, valid_seqs={1, 2, 3}, existing=[])

        self.assertEqual(len(topics), 1)
        entry = topics[0]["entry"]
        self.assertEqual(topics[0]["title"], "移動停利規則")
        self.assertEqual([i["text"] for i in entry["decisions"]], ["啟動點 +8%"])
        self.assertEqual(entry["turns"], [1, 2])
        self.assertEqual(dropped, 3)

    def test_a_matter_naming_an_existing_one_continues_it_and_keeps_its_title(self) -> None:
        existing = [{"id": 7, "title": "移動停利規則", "entries": [], "last_date": DAY1}]
        raw = {"topics": [{"match": 7, "title": "改了名字", "state": "新現況", "facts": [_item("事實", 3)]}]}

        topics, _ = ground_topics(raw, digest_date=DAY2, valid_seqs={3}, existing=existing)

        self.assertEqual((topics[0]["topic_id"], topics[0]["title"]), (7, "移動停利規則"))

    def test_an_unknown_id_or_a_repeated_title_does_not_duplicate_a_matter(self) -> None:
        existing = [{"id": 7, "title": "移動停利：規則", "entries": [], "last_date": DAY1}]
        raw = {
            "topics": [
                {"match": 99, "title": "移動停利 規則", "state": "a", "facts": [_item("f", 1)]},
                {"match": 99, "title": "全新的事", "state": "b", "facts": [_item("g", 2)]},
                {"match": None, "title": "全新的事", "state": "c", "facts": [_item("h", 3)]},
            ]
        }

        topics, _ = ground_topics(raw, digest_date=DAY2, valid_seqs={1, 2, 3}, existing=existing)

        self.assertEqual([(t["topic_id"], t["title"]) for t in topics], [(7, "移動停利：規則"), (None, "全新的事")])
        self.assertEqual([i["text"] for i in topics[1]["entry"]["facts"]], ["g", "h"])  # merged, one matter

    def test_titles_are_cleaned(self) -> None:
        self.assertEqual(clean_title("標題：「修正登入逾時」\n"), "修正登入逾時")
        self.assertEqual(len(clean_title("字" * 90)), 41)

    def test_garbage_answers_yield_nothing(self) -> None:
        for raw in ({}, {"topics": "x"}, {"topics": [1, None, {"state": "s"}]}):
            self.assertEqual(ground_topics(raw, digest_date=DAY1, valid_seqs={1}, existing=[])[0], [])


class RenderTests(TestCase):
    def test_body_is_organised_with_state_open_items_and_dated_history(self) -> None:
        topic = {
            "first_date": DAY1,
            "entries": [
                {"date": DAY1, "state": "先用固定停利", "decisions": [_item("停利 5%", 4, 5)], "facts": [], "open_items": [_item("要不要加 ATR", 6)]},
                {"date": DAY2, "state": "改成移動停利", "decisions": [_item("移動停利 +8% 啟動", 10, 11, 12)], "facts": [_item("回測勝率 52%", 14)], "open_items": []},
            ],
        }

        body = render_topic_markdown(topic)

        self.assertTrue(body.startswith(f"**現況（{DAY2}）**：改成移動停利"))
        self.assertNotIn("## 未決", body)  # the latest entry has none open: the old one is resolved
        self.assertIn("- 10-08｜停利 5%（turn #4–#5）", body)
        self.assertIn("- 10-09｜移動停利 +8% 啟動（turn #10–#12）", body)
        self.assertIn("## 歷程", body)
        self.assertLess(body.index(f"- {DAY2}：改成移動停利"), body.index(f"- {DAY1}：先用固定停利"))
        self.assertNotIn("Consolidated primary session transcript", body)


class PipelineTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.t1 = _add(self.store, "user", "移動停利要幾趴啟動？", "2026-10-08T01:00:00+00:00")
        self.t2 = _add(self.store, "assistant", "建議 +8% 啟動", "2026-10-08T01:01:00+00:00")
        self.t3 = _add(self.store, "user", "回測資料抓不到法人籌碼", "2026-10-08T02:00:00+00:00")
        self.t4 = _add(self.store, "user", "移動停利就用 +8%，回撤 6×ATR", "2026-10-08T03:00:00+00:00")
        self.t5 = _add(self.store, "user", "今天繼續移動停利，加上 ATR 上限", "2026-10-09T01:00:00+00:00")
        self.seqs = {t["id"]: t["turn_seq"] for t in self.store.get_primary_session_turns(SESSION)}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _day1_answer(self) -> dict:
        s = self.seqs
        return {
            "topics": [
                {"match": None, "title": "移動停利規則", "state": "+8% 啟動、回撤 6×ATR 出場",
                 "decisions": [_item("+8% 啟動", s[self.t1], s[self.t2], s[self.t4])]},
                {"match": None, "title": "法人籌碼資料抓取失敗", "state": "回測缺法人資料",
                 "open_items": [_item("找出法人資料來源", s[self.t3])]},
            ]
        }

    def test_the_prompt_lists_the_known_matters_and_the_day_but_not_other_days(self) -> None:
        call, calls = _model(self._day1_answer())
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)
        call2, calls2 = _model({"topics": []})

        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY2, TZ, call2)

        self.assertIn("移動停利規則", calls2[0])
        self.assertIn("今天繼續移動停利", calls2[0])
        self.assertNotIn("回測資料抓不到法人籌碼", calls2[0].split("=== Conversation log")[1])

    def test_a_day_is_split_into_matters_and_a_later_day_updates_the_same_one(self) -> None:
        call, _ = _model(self._day1_answer())
        self.assertEqual(run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call), 2)
        topic = next(t for t in self.store.list_memory_topics("proj", "ws_mem") if t["title"] == "移動停利規則")
        call2, _ = _model(
            {"topics": [{"match": topic["id"], "state": "加上 ATR 上限", "decisions": [_item("ATR 上限 10%", self.seqs[self.t5])]}]}
        )

        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY2, TZ, call2)

        topics = self.store.list_memory_topics("proj", "ws_mem")
        self.assertEqual(len(topics), 2)  # nothing duplicated
        updated = self.store.get_memory_topic(topic["id"])
        self.assertEqual([e["date"] for e in updated["entries"]], [DAY1, DAY2])
        self.assertEqual((updated["first_date"], updated["last_date"]), (DAY1, DAY2))

    def test_rerunning_a_day_replaces_its_entry_instead_of_adding_one(self) -> None:
        call, _ = _model(self._day1_answer())
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)
        topic = self.store.list_memory_topics("proj", "ws_mem")[-1]
        call2, _ = _model({"topics": [{"match": topic["id"], "state": "改寫", "facts": [_item("f", self.seqs[self.t1])]}]})

        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call2)

        self.assertEqual(len(self.store.get_memory_topic(topic["id"])["entries"]), 1)

    def test_sync_creates_then_updates_in_place_and_marks_the_day_archived(self) -> None:
        client = MagicMock()
        client.create_node.side_effect = ["mem_a", "mem_b"]
        call, _ = _model(self._day1_answer())
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)

        self.assertEqual(sync_topics_to_memtrace(self.store, client, "proj", "ws_mem"), 2)

        created = [c.kwargs for c in client.create_node.call_args_list]
        self.assertEqual([c["title"] for c in created], ["移動停利規則", "法人籌碼資料抓取失敗"])
        self.assertEqual(created[0]["tags"], ["harness", "draft", "topic"])
        self.assertNotIn("Turn #", created[0]["body"])  # organised, never a transcript
        self.assertNotIn("移動停利要幾趴啟動？", created[0]["body"])
        self.assertEqual(len(self.store.get_unconsolidated_turns(SESSION)), 5)
        self.assertEqual(finish_archived_days(self.store, "proj", "ws_mem", TZ), [DAY1])
        remaining = [t["turn_seq"] for t in self.store.get_unconsolidated_turns(SESSION)]
        self.assertEqual(remaining, [self.seqs[self.t5]])  # today's (the next day's) turns wait
        self.assertEqual(sync_topics_to_memtrace(self.store, client, "proj", "ws_mem"), 0)  # clean now

        first = self.store.list_memory_topics("proj", "ws_mem")[-1]
        call2, _ = _model({"topics": [{"match": first["id"], "state": "更新", "facts": [_item("f", self.seqs[self.t5])]}]})
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY2, TZ, call2)
        client.reset_mock()
        sync_topics_to_memtrace(self.store, client, "proj", "ws_mem")
        client.create_node.assert_not_called()
        self.assertEqual(client.update_node.call_args.kwargs["node_id"], first["node_id"] or "mem_a")

    def test_a_failed_write_leaves_the_day_unarchived_and_is_retried(self) -> None:
        client = MagicMock()
        client.create_node.side_effect = RuntimeError("MemTrace down")
        call, _ = _model(self._day1_answer())
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)

        with self.assertRaises(RuntimeError):
            sync_topics_to_memtrace(self.store, client, "proj", "ws_mem")

        self.assertEqual(finish_archived_days(self.store, "proj", "ws_mem", TZ), [])
        self.assertEqual(len(self.store.get_unconsolidated_turns(SESSION)), 5)
        client.create_node.side_effect = ["mem_a", "mem_b"]
        self.assertEqual(sync_topics_to_memtrace(self.store, client, "proj", "ws_mem"), 2)
        self.assertEqual(finish_archived_days(self.store, "proj", "ws_mem", TZ), [DAY1])

    def test_a_day_with_nothing_worth_keeping_is_still_done(self) -> None:
        call, _ = _model({"topics": []})
        self.assertEqual(run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call), 0)
        self.assertEqual(self.store.topic_run_dates("proj"), {DAY1})
        self.assertEqual(finish_archived_days(self.store, "proj", "ws_mem", TZ), [DAY1])

    def test_a_model_failure_records_nothing(self) -> None:
        def broken(prompt: str):
            raise DigestError("no candidate answered")

        with self.assertRaises(DigestError):
            run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, broken)
        self.assertEqual(self.store.topic_run_dates("proj"), set())

    def test_due_dates_are_finished_days_not_yet_run_oldest_first(self) -> None:
        now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(due_topic_dates(self.store, "proj", SESSION, TZ, now, max_days_back=None), [DAY1])
        call, _ = _model({"topics": []})
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)
        self.assertEqual(due_topic_dates(self.store, "proj", SESSION, TZ, now, max_days_back=None), [])

    def test_the_digest_links_to_the_matters_of_its_day(self) -> None:
        client = MagicMock()
        client.create_node.side_effect = ["mem_a", "mem_b"]
        call, _ = _model(self._day1_answer())
        run_topics_for_date(self.store, "proj", SESSION, "ws_mem", DAY1, TZ, call)
        sync_topics_to_memtrace(self.store, client, "proj", "ws_mem")

        _link_digest(self.store, client, "proj", "ws_mem", {"memtrace_node_id": "mem_digest", "digest_date": DAY1}, TZ)

        linked = sorted(c.kwargs["to_id"] for c in client.create_edge.call_args_list if c.kwargs["relation"] == "extracted_from")
        self.assertEqual(linked, ["mem_a", "mem_b"])

    def test_prompt_states_the_rules_a_reader_needs(self) -> None:
        prompt = build_topics_prompt("proj", DAY1, self.store.get_primary_session_turns(SESSION)[:2], [])
        for needle in ("one matter per subject", "MUST cite the turn numbers", "(none yet)", "No date"):
            self.assertIn(needle, prompt)
