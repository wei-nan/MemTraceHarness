from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.topic_recall import (
    RecallError,
    TopicRecallService,
    render_briefs_context,
    run_topic_recall,
)
from memtrace_harness.trace_store import TraceStore

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
HIT = {
    "node_id": "mem_aaa",
    "workspace_id": "ws_mem",
    "title": "Daily digest: P 2026-09-20",
    "created_at": "2026-09-20",
    "excerpt": "討論過訂閱驗證",
}


def _model(answer: dict | str):
    text = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
    return lambda prompt: (text, "claude", "claude-sonnet-5-5")


def _related(node_id="mem_aaa", why="當時決定過驗證流程"):
    return {"node_id": node_id, "workspace_id": "ws_mem", "title": "t", "why": why, "date": "2026-09-20"}


class RecallBase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = TraceStore(Path(tmp.name) / "t.sqlite3")

    def run_recall(self, answer, *, hits=(HIT,), verify=None, message="訂閱驗證要怎麼做", now=NOW):
        return run_topic_recall(
            self.store,
            "P",
            message=message,
            recent_transcript="",
            source_turns=[7],
            search=lambda q: list(hits),
            verify=verify,
            call_model=_model(answer),
            memory_workspace_id="ws_mem",
            spec_workspace_id="ws_spec",
            ttl_hours=72,
            now=now,
        )

    def active(self, now=NOW):
        return self.store.list_active_topic_briefs("P", now.isoformat())


class RunTopicRecallTests(RecallBase):
    def test_new_topic_with_history_stores_related_and_pushes(self) -> None:
        out = self.run_recall(
            {"decision": "new", "title": "訂閱驗證", "summary": "討論訂閱驗證", "update": True,
             "related": [_related()], "push": True, "push_text": "9/20 談過驗證流程"}
        )
        self.assertEqual(out.action, "new")
        self.assertIn("9/20 談過驗證流程", out.push_text)
        self.assertIn("mem_aaa", out.push_text)
        brief = self.active()[0]
        self.assertFalse(brief["fresh"])
        self.assertEqual(brief["related"][0]["node_id"], "mem_aaa")
        self.assertEqual(brief["provider"], "claude")

    def test_new_topic_without_history_is_fresh_and_silent(self) -> None:
        out = self.run_recall(
            {"decision": "new", "title": "新主題", "summary": "全新", "related": [], "push": True, "push_text": "x"},
            hits=(),
        )
        self.assertIsNone(out.push_text)
        self.assertTrue(self.active()[0]["fresh"])

    def test_fabricated_node_is_dropped_and_blocks_the_push(self) -> None:
        out = self.run_recall(
            {"decision": "new", "title": "T", "summary": "S", "related": [_related("mem_fake")],
             "push": True, "push_text": "編的"},
            verify=lambda ws, nid: None,
        )
        self.assertIsNone(out.push_text)
        brief = self.active()[0]
        self.assertEqual(brief["related"], [])
        self.assertTrue(brief["fresh"])

    def test_node_found_by_the_model_is_kept_only_when_it_exists(self) -> None:
        out = self.run_recall(
            {"decision": "new", "title": "T", "summary": "S", "related": [_related("mem_tool")],
             "push": True, "push_text": "有"},
            hits=(),
            verify=lambda ws, nid: {"title": "from tool"} if nid == "mem_tool" else None,
        )
        self.assertIsNotNone(out.push_text)
        self.assertEqual(self.active()[0]["related"][0]["node_id"], "mem_tool")

    def test_related_without_a_reason_is_dropped(self) -> None:
        self.run_recall(
            {"decision": "new", "title": "T", "summary": "S", "related": [_related(why="")]}
        )
        self.assertEqual(self.active()[0]["related"], [])

    def test_none_changes_nothing(self) -> None:
        out = self.run_recall({"decision": "none"}, message="好的謝謝你")
        self.assertEqual(out.action, "none")
        self.assertEqual(self.active(), [])

    def test_continue_without_update_only_extends_the_brief(self) -> None:
        self.run_recall({"decision": "new", "title": "T", "summary": "S", "related": []}, hits=())
        brief = self.active()[0]
        later = NOW + timedelta(hours=60)
        out = self.run_recall(
            {"decision": "continue", "brief_id": brief["id"], "update": False}, now=later
        )
        self.assertEqual(out.action, "continue")
        self.assertEqual(len(self.active(now=later + timedelta(hours=60))), 1)

    def test_continue_merges_related_and_pushes_only_new_finds(self) -> None:
        self.run_recall(
            {"decision": "new", "title": "T", "summary": "S", "related": [_related("mem_aaa")]}
        )
        brief = self.active()[0]
        second = dict(HIT, node_id="mem_bbb", title="另一份")
        out = self.run_recall(
            {"decision": "continue", "brief_id": brief["id"], "update": True, "summary": "S2",
             "related": [_related("mem_aaa"), _related("mem_bbb", "也相關")],
             "push": True, "push_text": "又找到一份"},
            hits=(HIT, second),
        )
        self.assertIn("mem_bbb", out.push_text)
        self.assertNotIn("mem_aaa", out.push_text)
        updated = self.active()[0]
        self.assertEqual(updated["summary"], "S2")
        self.assertEqual({r["node_id"] for r in updated["related"]}, {"mem_aaa", "mem_bbb"})

    def test_continue_with_unknown_brief_is_treated_as_new(self) -> None:
        out = self.run_recall(
            {"decision": "continue", "brief_id": 999, "update": True, "title": "T", "summary": "S",
             "related": []}
        )
        self.assertEqual(out.action, "new")
        self.assertEqual(len(self.active()), 1)

    def test_unusable_answers_raise(self) -> None:
        with self.assertRaises(RecallError):
            self.run_recall("not json at all")
        with self.assertRaises(RecallError):
            self.run_recall({"decision": "maybe"})
        with self.assertRaises(RecallError):
            self.run_recall({"decision": "new", "title": "", "summary": ""})

    def test_search_failure_does_not_stop_the_run(self) -> None:
        def boom(q):
            raise RuntimeError("memtrace down")

        out = run_topic_recall(
            self.store, "P", message="訂閱驗證要怎麼做", recent_transcript="", source_turns=[1],
            search=boom, verify=None,
            call_model=_model({"decision": "new", "title": "T", "summary": "S", "related": []}),
            memory_workspace_id="ws_mem", spec_workspace_id="ws_spec", ttl_hours=72, now=NOW,
        )
        self.assertEqual(out.action, "new")


class BriefLifetimeAndContextTests(RecallBase):
    def test_brief_expires_unless_the_topic_comes_back(self) -> None:
        self.run_recall({"decision": "new", "title": "T", "summary": "S", "related": []}, hits=())
        self.assertEqual(len(self.active(NOW + timedelta(hours=71))), 1)
        self.assertEqual(self.active(NOW + timedelta(hours=73)), [])

    def test_context_is_empty_with_nothing_to_say(self) -> None:
        self.assertEqual(render_briefs_context(self.store, "P", NOW), "")

    def test_context_shows_current_brief_in_full_and_marks_pending(self) -> None:
        real_now = datetime.now(timezone.utc)
        self.run_recall(
            {"decision": "new", "title": "訂閱驗證", "summary": "討論訂閱驗證", "related": [_related()]},
            now=real_now,
        )
        self.store.add_recall_pending("P", 1)
        text = render_briefs_context(self.store, "P", real_now + timedelta(minutes=5))
        self.assertIn("整理中", text)
        self.assertIn("訂閱驗證", text)
        self.assertIn("mem_aaa", text)
        self.assertRegex(text, r"[45] 分鐘前")

    def test_fresh_topic_is_labelled_as_having_no_history(self) -> None:
        self.run_recall({"decision": "new", "title": "T", "summary": "S", "related": []}, hits=())
        self.assertIn("全新主題", render_briefs_context(self.store, "P", NOW))

    def test_pending_counter_never_goes_negative(self) -> None:
        self.assertEqual(self.store.add_recall_pending("P", -1), 0)
        self.assertEqual(self.store.add_recall_pending("P", 2), 2)


class ServiceTests(RecallBase):
    def _service(self, answer, **config_overrides):
        config = MagicMock()
        config.recall_enabled = config_overrides.get("enabled", True)
        config.recall_candidates_for.return_value = (("claude", "m"),)
        config.memory_workspace_id_for.return_value = "ws_mem"
        config.recall_ttl_hours = 72
        client = MagicMock()
        client.search_nodes.return_value = [{"id": "mem_aaa", "title": "Daily digest", "body_excerpt_200": "x"}]
        self.sent, self.recorded = [], []
        service = TopicRecallService(
            config, self.store, client,
            call_model_factory=lambda project: _model(answer),
            send=lambda chat_id, text: self.sent.append((chat_id, text)) or True,
            record_push=lambda project, text: self.recorded.append((project, text)),
        )
        scope = MagicMock()
        scope.name = "P"
        scope.workspace_id = "ws_spec"
        return service, scope

    def test_short_or_disabled_messages_never_start_a_run(self) -> None:
        service, scope = self._service({"decision": "none"})
        self.assertFalse(service.start(scope, 1, "好"))
        service, scope = self._service({"decision": "none"}, enabled=False)
        self.assertFalse(service.start(scope, 1, "這是一則夠長的訊息"))
        self.assertEqual(self.store.get_recall_state("P")["pending"], 0)

    def test_run_pushes_records_and_clears_pending(self) -> None:
        service, scope = self._service(
            {"decision": "new", "title": "訂閱驗證", "summary": "S",
             "related": [_related()], "push": True, "push_text": "9/20 談過"}
        )
        self.store.add_recall_pending("P", 1)
        service._run(scope, 42, "訂閱驗證要怎麼做")
        self.assertEqual(self.sent[0][0], 42)
        self.assertIn("9/20 談過", self.sent[0][1])
        self.assertEqual(self.recorded[0][0], "P")
        state = self.store.get_recall_state("P")
        self.assertEqual(state["pending"], 0)
        self.assertIsNone(state["last_error"])
        self.assertIsNotNone(self.active_now()[0]["pushed_at"])

    def test_failed_run_is_recorded_and_clears_pending(self) -> None:
        service, scope = self._service("garbage")
        self.store.add_recall_pending("P", 1)
        service._run(scope, 42, "訂閱驗證要怎麼做")
        state = self.store.get_recall_state("P")
        self.assertEqual(state["pending"], 0)
        self.assertIn("RecallError", state["last_error"])
        self.assertEqual(self.sent, [])

    def active_now(self):
        return self.store.list_active_topic_briefs("P", datetime.now(timezone.utc).isoformat())


class RecallConfigTests(TestCase):
    def _config(self, **kw):
        from memtrace_harness.config import HarnessConfig

        return HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=Path("t"), trace_root=Path("."),
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900, telegram_bot_token=None,
            telegram_allowed_chat_ids=set(), project_index_path=None, chat_provider="claude",
            chat_model="haiku", unattended_write_requires_approval=True, **kw,
        )

    def test_recall_falls_back_to_the_digest_chain_then_chat(self) -> None:
        self.assertEqual(self._config().recall_candidates_for("p"), (("claude", "haiku"),))
        config = self._config(digest_provider="claude", digest_model="claude-sonnet-5-5")
        self.assertEqual(config.recall_candidates_for("p"), (("claude", "claude-sonnet-5-5"),))

    def test_own_recall_model_wins_and_does_not_degrade(self) -> None:
        config = self._config(
            digest_provider="claude", digest_model="sonnet",
            recall_provider="codex", recall_model="gpt-5.6-sol", recall_fallbacks=(("claude", "opus"),),
        )
        self.assertEqual(
            config.recall_candidates_for("p"), (("codex", "gpt-5.6-sol"), ("claude", "opus"))
        )
