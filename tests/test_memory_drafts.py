from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from memtrace_harness import memory_drafts
from memtrace_harness.memory_digest import sync_digests_to_memtrace
from memtrace_harness.memory_drafts import (
    apply_draft_retitle,
    draft_title,
    parse_turn_seqs,
    plan_draft_retitle,
)
from memtrace_harness.memtrace_client import MemTraceClient, MemTraceClientError
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.trace_store import TraceStore

TZ = ZoneInfo("Asia/Taipei")


def _store(tmp: str) -> TraceStore:
    return TraceStore(Path(tmp) / "t.sqlite3")


def _add(store: TraceStore, speaker: str, content: str, at: str, turn_type: str = "chat") -> int:
    turn_id = store.append_primary_session_turn(
        primary_session_id="psess_proj", project="proj", speaker=speaker, turn_type=turn_type, content=content
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (at, turn_id))
    return turn_id


class DraftTitleTests(TestCase):
    def test_title_says_when_which_turns_and_how_it_opens(self) -> None:
        title = draft_title(
            "Beri",
            [
                (419, "2026-09-30T06:30:00+00:00", "assistant", "好的"),
                (412, "2026-09-30T06:05:00+00:00", "user", "請接續上次的任務，我想先確認一下進度，然後再決定要不要繼續往下做"),
            ],
            TZ,
        )
        self.assertTrue(title.startswith("Draft: Beri 2026-09-30 14:05（turns #412–#419）· 請接續上次的任務"))
        self.assertTrue(title.endswith("…"))  # the opener is cut at 24 characters

    def test_single_turn_no_user_turn_and_quote_suffix(self) -> None:
        self.assertEqual(
            draft_title("p", [(7, "2026-09-30T00:00:00+00:00", "system", "排程 sched_a 觸發")], TZ),
            "Draft: p 2026-09-30 08:00（turn #7）· 排程 sched_a 觸發",
        )
        quoted = "這檔呢\n（回覆的訊息：「建議入場，開盤漲幅 3.2%」）"
        self.assertTrue(draft_title("p", [(1, "2026-09-30T00:00:00+00:00", "user", quoted)], TZ).endswith("· 這檔呢"))

    def test_titles_of_different_batches_differ(self) -> None:
        a = draft_title("p", [(1, "2026-09-30T00:00:00+00:00", "user", "same")], TZ)
        b = draft_title("p", [(2, "2026-09-30T01:00:00+00:00", "user", "same")], TZ)
        self.assertNotEqual(a, b)

    def test_parse_turn_seqs_only_reads_line_starts(self) -> None:
        body = "Consolidated primary session transcript for project p:\n\nTurn #3 [user]: hi\nTurn #5 [assistant]: 見 Turn #9 [x]\nTurn #4 [user]: yo"
        self.assertEqual(parse_turn_seqs(body), [3, 4, 5])


class LiveWriteTests(TestCase):
    def test_hourly_archive_uses_the_new_title_and_remembers_the_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = _store(tmp)
            client = MagicMock()
            client.create_node.return_value = "mem_new"
            mgr = PrimarySessionManager(store, client)
            mgr.record_turn(project="proj", speaker="user", turn_type="decision", content="決定資料期間三年")
            mgr.record_turn(project="proj", speaker="work_session_report", turn_type="dev_report", content="完成")

            mgr.consolidate_to_memtrace("proj", "ws_mem", tz=TZ)

            title = client.create_node.call_args.kwargs["title"]
            self.assertRegex(title, r"^Draft: proj \d{4}-\d{2}-\d{2} \d{2}:\d{2}（turns #1–#2）· 決定資料期間三年$")
            self.assertEqual(
                store.list_memory_drafts("proj", "ws_mem"),
                [{"node_id": "mem_new", "first_turn_seq": 1, "last_turn_seq": 2, "created_at": store.list_memory_drafts("proj", "ws_mem")[0]["created_at"]}],
            )


class RetitleTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _store(self._tmp.name)
        _add(self.store, "user", "第一天看盤", "2026-09-29T01:00:00+00:00")
        _add(self.store, "assistant", "收到", "2026-09-29T01:01:00+00:00")
        _add(self.store, "user", "第二天繼續", "2026-09-30T02:00:00+00:00")
        self.client = MagicMock(spec=MemTraceClient)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _nodes(self) -> list[dict]:
        old = "Draft primary session consolidation: proj"
        return [
            {"id": "mem_a", "title": old, "body": "Consolidated ...:\n\nTurn #1 [user]: 第一天看盤\nTurn #2 [assistant]: 收到"},
            {"id": "mem_b", "title": old, "body": "Consolidated ...:\n\nTurn #3 [user]: 第二天繼續"},
            {"id": "mem_gone", "title": old, "body": "Turn #99 [user]: not in the local log"},
            {"id": "mem_empty", "title": old, "body": "nothing parseable"},
            {"id": "mem_spec", "title": "小說世界觀設定", "body": "Turn #1 [user]: 不是草稿"},
            {"id": "mem_done", "title": "Draft: proj 2026-09-29 09:00（turns #1–#2）· x", "body": "Turn #1 [user]: x"},
        ]

    def test_plan_is_read_only_and_covers_only_old_style_drafts(self) -> None:
        self.client.list_nodes.return_value = self._nodes()

        with patch.object(memory_drafts.time, "sleep"):
            plans, skipped = plan_draft_retitle(self.client, self.store, "proj", "ws_mem", TZ)

        self.assertEqual([p.node_id for p in plans], ["mem_a", "mem_b"])
        self.assertEqual(plans[0].new_title, "Draft: proj 2026-09-29 09:00（turns #1–#2）· 第一天看盤")
        self.assertEqual(plans[1].new_title, "Draft: proj 2026-09-30 10:00（turn #3）· 第二天繼續")
        self.assertEqual({k.node_id for k in skipped}, {"mem_gone", "mem_empty"})
        self.client.update_node.assert_not_called()
        self.assertEqual(self.store.list_memory_drafts("proj", "ws_mem"), [])

    def test_apply_renames_and_records_the_turn_ranges(self) -> None:
        self.client.list_nodes.return_value = self._nodes()
        with patch.object(memory_drafts.time, "sleep"):
            plans, _ = plan_draft_retitle(self.client, self.store, "proj", "ws_mem", TZ)
            done = apply_draft_retitle(self.client, self.store, "proj", "ws_mem", plans, log=lambda m: None)

        self.assertEqual(done, 2)
        titles = {c.kwargs["node_id"]: c.kwargs["title"] for c in self.client.update_node.call_args_list}
        self.assertEqual(titles["mem_a"], plans[0].new_title)
        self.assertNotIn("body", self.client.update_node.call_args_list[0].kwargs)  # title only
        ranges = {d["node_id"]: (d["first_turn_seq"], d["last_turn_seq"]) for d in self.store.list_memory_drafts("proj", "ws_mem")}
        self.assertEqual(ranges, {"mem_a": (1, 2), "mem_b": (3, 3)})

    def test_a_rate_limit_is_waited_out_instead_of_aborting(self) -> None:
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MemTraceClientError("MemTrace HTTP 429: Rate limit exceeded")
            return None

        with patch.object(memory_drafts.time, "sleep") as sleep:
            memory_drafts._throttled(flaky)

        self.assertEqual(calls["n"], 2)
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], memory_drafts.BACKOFF_SECONDS)


class CreateEdgeClientTests(TestCase):
    def _client(self, outcome) -> MemTraceClient:
        client = MemTraceClient("http://x", "t")
        return patch.object(MemTraceClient, "call_tool", side_effect=outcome) and client

    def test_existing_edge_counts_as_done_other_errors_raise(self) -> None:
        client = MemTraceClient("http://x", "t")
        with patch.object(MemTraceClient, "call_tool", return_value={}) as call:
            self.assertTrue(client.create_edge(workspace_id="w", from_id="a", to_id="b", relation="extends"))
            self.assertEqual(call.call_args.args[1], {"workspace_id": "w", "from_id": "a", "to_id": "b", "relation": "extends"})
        with patch.object(
            MemTraceClient, "call_tool",
            side_effect=MemTraceClientError("{'code': -32603, 'message': '409: Edge with this relation already exists'}"),
        ):
            self.assertFalse(client.create_edge(workspace_id="w", from_id="a", to_id="b", relation="extends"))
        with patch.object(MemTraceClient, "call_tool", side_effect=MemTraceClientError("404: Node not found")):
            with self.assertRaises(MemTraceClientError):
                client.create_edge(workspace_id="w", from_id="a", to_id="b", relation="extends")


class DigestEdgeTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _store(self._tmp.name)
        # day 1 = turns 1-3, day 2 = turns 4-5 (local Taipei dates)
        for at in ("2026-09-29T01:00:00+00:00", "2026-09-29T02:00:00+00:00", "2026-09-29T03:00:00+00:00",
                   "2026-09-30T01:00:00+00:00", "2026-09-30T02:00:00+00:00"):
            _add(self.store, "user", "x", at)
        for node, first, last in (("mem_d1", 1, 2), ("mem_d1b", 3, 4), ("mem_d2", 5, 5)):
            self.store.record_memory_draft(project="proj", workspace_id="ws_mem", node_id=node, first_turn_seq=first, last_turn_seq=last)
        for day in ("2026-09-29", "2026-09-30"):
            self.store.save_memory_digest(
                project="proj", digest_date=day, provider=None, model=None, turn_count=1,
                digest={"summary": f"s {day}", "decisions": [], "open_items": []},
            )
        self.client = MagicMock()
        self.client.create_node.side_effect = ["mem_digest1", "mem_digest2", "mem_digest_again"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _edges(self) -> set[tuple[str, str, str]]:
        return {
            (c.kwargs["from_id"], c.kwargs["to_id"], c.kwargs["relation"])
            for c in self.client.create_edge.call_args_list
        }

    def test_digest_extends_the_previous_one_and_points_at_its_days_drafts(self) -> None:
        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)

        self.assertEqual(
            self._edges(),
            {
                ("mem_digest1", "mem_d1", "extracted_from"),
                ("mem_digest1", "mem_d1b", "extracted_from"),  # draft 3-4 straddles both days
                ("mem_digest2", "mem_digest1", "extends"),
                ("mem_digest2", "mem_d1b", "extracted_from"),
                ("mem_digest2", "mem_d2", "extracted_from"),
            },
        )
        self.assertNotIn(("mem_digest1", "mem_digest2", "extends"), self._edges())

    def test_a_failed_link_is_retried_without_recreating_the_node(self) -> None:
        self.client.create_edge.side_effect = MemTraceClientError("MemTrace HTTP 500")
        with self.assertRaises(MemTraceClientError):
            sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)
        self.client.create_edge.side_effect = None
        self.client.create_edge.reset_mock()

        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)

        self.assertEqual(self.client.create_node.call_count, 2)  # nodes were not written twice
        self.assertIn(("mem_digest1", "mem_d1", "extracted_from"), self._edges())
        # and once everything is linked, nothing more happens
        self.client.create_edge.reset_mock()
        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)
        self.client.create_edge.assert_not_called()

    def test_a_regenerated_digest_updates_its_node_in_place_and_keeps_its_edges(self) -> None:
        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)
        self.client.create_edge.reset_mock()
        self.store.save_memory_digest(
            project="proj", digest_date="2026-09-30", provider=None, model=None, turn_count=2,
            digest={"summary": "revised", "decisions": [], "open_items": []},
        )

        written = sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)

        self.assertEqual(written, 1)
        self.assertEqual(self.client.create_node.call_count, 2)  # no third node
        kwargs = self.client.update_node.call_args.kwargs
        self.assertEqual(kwargs["node_id"], "mem_digest2")
        self.assertIn("revised", kwargs["body"])
        self.assertEqual(kwargs["title"], "Daily digest: proj 2026-09-30")
        self.client.create_edge.assert_not_called()

    def test_moving_to_a_new_workspace_creates_fresh_nodes_and_links_there(self) -> None:
        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_mem", TZ)
        self.client.create_edge.reset_mock()
        self.client.create_node.side_effect = ["n1", "n2"]

        sync_digests_to_memtrace(self.store, self.client, "proj", "ws_new", TZ)

        self.assertIn(("n2", "n1", "extends"), self._edges())
        # drafts live in the old workspace, so nothing is linked across workspaces
        self.assertFalse([e for e in self._edges() if e[2] == "extracted_from"])
