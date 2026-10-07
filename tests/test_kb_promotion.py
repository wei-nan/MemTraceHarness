from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import cli
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.kb_gardening import undo_created_nodes
from memtrace_harness.kb_promotion import (
    MAX_NOTES,
    PromotionOutcome,
    describe_outcome,
    render_index,
    run_promotion_pass,
    source_digests,
    spec_knowledge,
    unsupported_numbers,
    validate_directions,
    validate_notes,
    verify_evidence,
)
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore

DIGEST_A = (
    "# Daily digest: P 2026-09-27\n\n## 知識與事實\n"
    "- 當日沖回測共 759 筆交易，勝率 48.75%，平均淨報酬 -0.75%；其中 308 筆（40.58%）觸及 2% 停利點。\n"
)
DIGEST_B = (
    "# Daily digest: P 2026-09-29\n\n## 知識與事實\n"
    "- 零股隔日沖過去 6 個月僅 17 筆交易，扣除 0.585% 交易成本後為負期望。\n"
)
Q_A = "當日沖回測共 759 筆交易，勝率 48.75%，平均淨報酬 -0.75%"
Q_B = "零股隔日沖過去 6 個月僅 17 筆交易"


def digest(i, title, body):
    return {"id": f"mem_d{i}", "title": title, "body": body, "tags": ["harness", "draft", "daily-digest"],
            "content_type": "context", "created_at": f"2026-09-{26 + i}T00:00:00Z", "resolution_status": "open"}


def spec_node(i, title="規格節點", **kw):
    n = {"id": f"mem_s{i}", "title": title, "body": f"內容 {i}", "tags": ["動能策略"], "content_type": "document",
         "created_at": "2026-09-30T00:00:00Z", "resolution_status": "open"}
    n.update(kw)
    return n


SOURCES = {d["id"]: d for d in (digest(1, "Daily digest: P 2026-09-27", DIGEST_A), digest(2, "Daily digest: P 2026-09-29", DIGEST_B))}


def note(**kw):
    n = {
        "title": "當日沖多因子回測為負期望",
        "body": "全市場多因子當日沖回測共 759 筆交易，勝率 48.75%，平均淨報酬 -0.75%，仍為負期望，只能作歷史診斷。",
        "tags": ["當日沖"], "links": [{"to_id": "mem_s1", "relation": "related_to"}],
        "evidence": [{"node_id": "mem_d1", "quote": Q_A}],
    }
    n.update(kw)
    return n


def direction(**kw):
    d = {"name": "當日沖多因子", "status": "擱置", "summary": "全市場當日沖。", "conclusion": "759 筆平均淨報酬 -0.75%，不足以採用。",
         "note_titles": ["當日沖多因子回測為負期望"], "existing_node_id": None, "evidence": [{"node_id": "mem_d1", "quote": Q_A}]}
    d.update(kw)
    return d


class FakeKb:
    def __init__(self, memory, spec):
        self.ws = {"ws_mem": {n["id"]: dict(n) for n in memory}, "ws_spec": {n["id"]: dict(n) for n in spec}}
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.deleted: list[str] = []
        self.edges: list[tuple] = []

    def list_nodes(self, *, workspace_id, limit=200, offset=0, stage=None):
        return list(self.ws.get(workspace_id, {}).values())[offset: offset + limit]

    def create_node(self, **kw):
        self.created.append(kw)
        node_id = f"mem_n{len(self.created)}"
        self.ws[kw["workspace_id"]][node_id] = {"id": node_id, "title": kw["title"], "body": kw["body"], "tags": kw["tags"]}
        return node_id

    def update_node(self, **kw):
        self.updated.append(kw)

    def create_edge(self, **kw):
        self.edges.append((kw["from_id"], kw["to_id"], kw["relation"]))
        return True

    def delete_node(self, **kw):
        self.deleted.append(kw["node_id"])


def kb():
    return FakeKb(list(SOURCES.values()), [spec_node(1), spec_node(2, "舊持倉")])


class EvidenceTests(TestCase):
    def test_a_quote_must_occur_verbatim_in_the_node_it_names(self) -> None:
        ok = verify_evidence([{"node_id": "mem_d1", "quote": Q_A}], SOURCES)
        self.assertEqual(len(ok), 1)
        # Re-wrapped whitespace and markdown decoration survive; different words do not.
        self.assertTrue(verify_evidence([{"node_id": "mem_d1", "quote": "當日沖回測共  759 筆交易，\n勝率 **48.75%**，平均淨報酬 -0.75%"}], SOURCES))
        for bad in (
            {"node_id": "mem_d1", "quote": "當日沖回測共 760 筆交易，勝率 48.75%，平均淨報酬 -0.75%"},   # a number changed
            {"node_id": "mem_d2", "quote": Q_A},                                                          # right words, wrong node
            {"node_id": "mem_nope", "quote": Q_A},
            {"node_id": "mem_d1", "quote": "759 筆"},                                                      # too short to prove anything
            "garbage",
        ):
            self.assertEqual(verify_evidence([bad], SOURCES), [], bad)

    def test_numbers_must_be_in_the_cited_sources(self) -> None:
        cited = [{"node_id": "mem_d1", "quote": Q_A}]
        self.assertEqual(unsupported_numbers("共 759 筆，勝率 48.75%，觸及 308 筆", cited, SOURCES), [])
        self.assertEqual(unsupported_numbers("共 1,759 筆，期望值 -1.20%", cited, SOURCES), ["1759", "-1.20"[1:]])
        # A number that only another digest contains is not supported by this item's own citation.
        self.assertEqual(unsupported_numbers("僅 17 筆", cited, SOURCES), ["17"])
        self.assertEqual(unsupported_numbers("共 3 檔", cited, SOURCES), [])   # single digits are not checked


class ValidationTests(TestCase):
    spec_ids = {"mem_s1", "mem_s2"}

    def test_notes_need_verified_evidence_supported_numbers_and_sane_text(self) -> None:
        notes, dropped = validate_notes(
            [
                note(),
                note(title="沒有引文", evidence=[]),
                note(title="假引文", evidence=[{"node_id": "mem_d1", "quote": "這句話根本不在摘要裡面喔喔喔"}]),
                note(title="算錯的數字", body="全市場多因子當日沖回測共 761 筆交易，勝率 48.75%，平均淨報酬 -0.75%，仍為負期望。"),
                note(title="太短", body="短"),
                note(),                                       # same title again
                "garbage",
            ],
            SOURCES, self.spec_ids,
        )
        self.assertEqual([n["title"] for n in notes], ["當日沖多因子回測為負期望"])
        self.assertEqual(len(dropped), 6)
        self.assertTrue(any("數字 761" in d for d in dropped))

    def test_links_only_to_nodes_that_exist_with_known_relations(self) -> None:
        [n], _ = validate_notes(
            [note(links=[{"to_id": "mem_s1", "relation": "related_to"}, {"to_id": "mem_zzz", "relation": "related_to"},
                         {"to_id": "mem_s2", "relation": "owns"}])],
            SOURCES, self.spec_ids,
        )
        self.assertEqual(n["links"], [{"to_id": "mem_s1", "relation": "related_to"}])

    def test_at_most_a_bounded_number_of_notes(self) -> None:
        many = [note(title=f"筆記 {i} 號", ) for i in range(MAX_NOTES + 5)]
        notes, _ = validate_notes(many, SOURCES, self.spec_ids)
        self.assertEqual(len(notes), MAX_NOTES)

    def test_directions_need_a_legal_status_and_grounding(self) -> None:
        keys = {"當日沖多因子回測為負期望"}
        rows, dropped = validate_directions(
            [
                direction(),
                direction(name="狀態亂寫", status="很棒"),
                direction(name="沒依據", evidence=[]),
                direction(name="指向既有節點", evidence=[], existing_node_id="mem_s1", conclusion="見規格文件"),
                direction(name="編造的節點", evidence=[], existing_node_id="mem_zzz"),
                direction(name="算錯", conclusion="共 999 筆", ),
            ],
            SOURCES, self.spec_ids, keys,
        )
        self.assertEqual([r["name"] for r in rows], ["當日沖多因子", "指向既有節點"])
        self.assertEqual(len(dropped), 4)
        self.assertEqual(rows[0]["note_titles"], ["當日沖多因子回測為負期望"])

    def test_the_overview_is_rendered_by_the_harness_with_pipes_escaped(self) -> None:
        rows, _ = validate_directions([direction(name="A|B 策略")], SOURCES, self.spec_ids, {"當日沖多因子回測為負期望"})
        text = render_index("P", rows, {"當日沖多因子回測為負期望": "mem_n1"}, SOURCES, "ws_mem", "2026-10-08")
        self.assertIn("| A／B 策略 | 擱置 |", text)
        self.assertIn("mem_n1", text)
        self.assertIn("ws_mem/mem_d1", text)
        self.assertIn(f"「{Q_A}」", text)

    def test_what_the_model_is_shown_of_the_spec_workspace_leaves_out_drafts_and_the_harnesss_own_nodes(self) -> None:
        shown = spec_knowledge([
            spec_node(1), spec_node(2, "Harness loop draft: 追蹤"), spec_node(3, tags=["charter"]),
            spec_node(4, tags=["directions-index"]),
        ])
        self.assertEqual([n["id"] for n in shown], ["mem_s1"])
        self.assertEqual([d["id"] for d in source_digests(list(SOURCES.values()) + [spec_node(9)])], ["mem_d1", "mem_d2"])


def store(tmp):
    return TraceStore(Path(tmp) / "t.sqlite3")


def reply(**kw):
    base = {"notes": [note()], "directions": [direction()], "stale_candidates": [], "summary": "整理了一則"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def run(client, ts, raw, **kw):
    return run_promotion_pass(
        client=client, trace_store=ts, project="P", spec_workspace_id="ws_spec", memory_workspace_id="ws_mem",
        purpose="交易專案", caller=lambda prompt: raw, **kw,
    )


class PassTests(TestCase):
    def test_a_pass_writes_notes_with_harness_written_provenance_and_a_pinned_overview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            out = run(k, ts, reply())
            self.assertEqual((out.created, out.index, out.index_rows), (["當日沖多因子回測為負期望"], "created", 1))
            created, overview = k.created
            self.assertNotIn("draft", created["tags"])
            self.assertEqual(created["tags"][:3], ["harness", "controller", "conclusion"])
            self.assertIn("## 出處（由 Harness 核對過原文）", created["body"])
            self.assertIn(f"「{Q_A}」", created["body"])
            self.assertIn("ws_mem/mem_d1", created["body"])
            self.assertEqual(overview["tags"], ["harness", "controller", "directions-index"])
            self.assertIn("研究與策略總覽：P", overview["title"])
            self.assertIn("mem_n1", overview["body"])
            self.assertTrue(any(u.get("pinned") for u in k.updated))
            self.assertIn(("mem_n1", "mem_s1", "related_to"), k.edges)
            self.assertIsNotNone(ts.get_workspace_index("ws_spec", "directions"))
            self.assertEqual(ts.get_gardening_run(out.run_id)["status"], "ok")

    def test_a_dry_run_writes_nothing_and_shows_what_it_would(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            out = run(k, ts, reply(), dry_run=True)
            self.assertTrue(out.dry_run)
            self.assertEqual(len(out.preview["notes"]), 1)
            self.assertEqual((k.created, k.updated, k.edges), ([], [], []))
            self.assertIsNone(ts.get_workspace_index("ws_spec", "directions"))
            self.assertIn("試跑", describe_outcome(out))

    def test_a_rerun_updates_instead_of_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            run(k, ts, reply())
            k.updated.clear()
            again = run(k, ts, reply())
            self.assertEqual((again.created, again.updated, again.unchanged, again.index), ([], [], 1, None))
            self.assertEqual(len(k.created), 2)                              # the note and the overview, once
            changed = note(body=note()["body"] + "後來又補充了一點說明文字。")
            third = run(k, ts, reply(notes=[changed]))
            self.assertEqual((third.created, third.updated), ([], ["當日沖多因子回測為負期望"]))
            self.assertEqual(len(k.created), 2)
            self.assertTrue(any(u["node_id"] == "mem_n1" and "補充" in u["body"] for u in k.updated))

    def test_unverifiable_items_are_dropped_and_reported_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            out = run(k, ts, reply(notes=[note(), note(title="編造", evidence=[{"node_id": "mem_d1", "quote": "沒有這句話但是看起來很像真的喔"}])]))
            self.assertEqual(out.created, ["當日沖多因子回測為負期望"])
            self.assertEqual(len(out.dropped), 1)
            self.assertIn("被擋下", describe_outcome(out))

    def test_stale_candidates_are_only_reported_for_nodes_that_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            out = run(k, ts, reply(stale_candidates=[
                {"node_id": "mem_s2", "reason": "持倉已出場"}, {"node_id": "mem_ghost", "reason": "不存在"}]))
            self.assertEqual(out.stale, [("mem_s2", "持倉已出場")])
            self.assertIn("只回報，沒動", describe_outcome(out))
            self.assertNotIn("mem_s2", [u["node_id"] for u in k.updated])

    def test_nothing_to_promote_from_means_no_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            caller = MagicMock()
            same = run_promotion_pass(client=kb(), trace_store=ts, project="P", spec_workspace_id="ws_spec",
                                      memory_workspace_id="ws_spec", purpose="", caller=caller)
            no_digests = run_promotion_pass(client=FakeKb([digest(1, "x", "y")] and [spec_node(5)], [spec_node(1)]),
                                            trace_store=ts, project="P", spec_workspace_id="ws_spec",
                                            memory_workspace_id="ws_mem", purpose="", caller=caller)
            self.assertIsNone(same)
            self.assertIsNone(no_digests)
            caller.assert_not_called()
            self.assertIsNone(run(kb(), ts, "not json"))

    def test_undo_removes_what_a_pass_created_and_a_rerun_may_write_it_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, k = store(tmp), kb()
            out = run(k, ts, reply())
            self.assertEqual(undo_created_nodes(k, ts, out.run_id), (2, 0))
            self.assertEqual(sorted(k.deleted), ["mem_n1", "mem_n2"])
            self.assertEqual(undo_created_nodes(k, ts, out.run_id), (0, 0))
            self.assertIsNone(ts.get_workspace_index("ws_spec", "directions"))
            self.assertEqual(ts.list_promotions("ws_spec"), [])
            again = run(k, ts, reply())
            self.assertEqual(again.created, ["當日沖多因子回測為負期望"])


def _gateway(tmp, kb_=None):
    path = Path(tmp)
    ts = TraceStore(path / "t.sqlite3")
    config = HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=path / "t.sqlite3", trace_root=path,
        claude_command="claude", codex_command="codex", antigravity_command="agy",
        antigravity_output_mode="auto", cli_timeout_seconds=900, telegram_bot_token="fake",
        telegram_allowed_chat_ids={12345}, project_index_path=None, chat_provider="claude",
        chat_model="haiku", unattended_write_requires_approval=True,
    )
    scope = ProjectScope(name="test_proj", workspace_id="ws_spec", working_directory=path,
                         scope_file_path=path / "harness-scope.md")
    gw = TelegramGateway(config, ApprovalManager(ts, {12345}), ChatTriage([scope]), PrimarySessionManager(ts),
                         [scope], memtrace_client=kb_)
    gw.send_message = MagicMock()
    gw.send_message_with_keyboard = MagicMock(return_value=3)
    gw.clear_message_keyboard = MagicMock()
    gw.answer_callback_query = MagicMock()
    return gw, ts, scope


class GatewayTests(TestCase):
    def test_the_report_has_an_undo_button_only_when_something_was_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.notify_promotion_outcome(PromotionOutcome(7, "test_proj", "ws_spec", created=["一則"]))
            text, keyboard = gw.send_message_with_keyboard.call_args.args[1:3]
            self.assertEqual(keyboard[0][0]["callback_data"], "kb_undo:7")
            self.assertIn("撤銷", text)
            gw.send_message_with_keyboard.reset_mock()
            gw.notify_promotion_outcome(PromotionOutcome(8, "test_proj", "ws_spec", unchanged=2))
            gw.send_message_with_keyboard.assert_not_called()

    def test_the_undo_button_takes_back_the_created_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k = kb()
            gw, ts, scope = _gateway(tmp, k)
            out = run(k, ts, reply())
            gw._handle_callback_query({"id": "cb", "data": f"kb_undo:{out.run_id}",
                                       "message": {"chat": {"id": 12345}, "message_id": 4}})
            self.assertEqual(sorted(k.deleted), ["mem_n1", "mem_n2"])
            self.assertIn("已撤銷 2 個新增的節點", gw.send_message.call_args.args[1])

    def test_the_overview_is_given_to_the_controller_and_the_team_as_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            ts.set_workspace_index("ws_spec", "directions", "mem_idx", "# 研究與策略總覽\n| 方向 | 狀態 |")
            [item] = [i for i in gw._project_context_items(scope) if i.ref.startswith("harness:kb-index:")]
            self.assertIn("研究與策略總覽", item.body)
            self.assertEqual(item.content_type, "context")

    def test_the_promote_command_starts_a_pass_for_that_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.promote_now = MagicMock()
            gw.process_update({"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/promote"}})
            gw.promote_now.assert_called_once_with(scope, 12345)
            gw.promote_now = None
            gw.process_update({"update_id": 2, "message": {"chat": {"id": 12345}, "text": "/promote"}})
            self.assertIn("沒有啟用", gw.send_message.call_args.args[1])


class CliTests(TestCase):
    def test_the_runner_promotes_reports_and_stays_quiet_without_a_knowledge_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            k = kb()
            gw, ts, scope = _gateway(tmp, k)
            scope.raw_markdown = "測試專案"
            gw.notify_promotion_outcome = MagicMock()
            with patch.object(gw.config.__class__, "memory_workspace_id_for", lambda self, name, ws: "ws_mem"), patch.object(
                cli, "make_review_caller", return_value=lambda prompt: reply()
            ):
                ran = cli.run_knowledge_promotion(gw.config, ts, k, [scope], {"test_proj": gw}, chat_id=12345)
            self.assertEqual(ran, 1)
            gw.notify_promotion_outcome.assert_called_once()
            self.assertEqual(cli.run_knowledge_promotion(gw.config, ts, None, [scope], {}), 0)
