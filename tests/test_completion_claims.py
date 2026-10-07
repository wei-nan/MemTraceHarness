from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.completion_claims import (
    KB_UPDATES_SCHEMA,
    STATUS_CLAIMED,
    STATUS_DONE,
    STATUS_OPEN,
    accept_claim,
    apply_kb_updates,
    normalize_kb_updates,
    reject_claim,
)
from memtrace_harness.decision_records import precedent_block
from memtrace_harness.output_contracts import output_schema_path
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.config import HarnessConfig


def _config(tmp: Path) -> HarnessConfig:
    return HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=tmp / "t.sqlite3",
        trace_root=tmp, claude_command="claude", codex_command="codex",
        antigravity_command="agy", antigravity_output_mode="auto", cli_timeout_seconds=900,
        telegram_bot_token="fake", telegram_allowed_chat_ids={12345}, project_index_path=None,
        chat_provider="claude", chat_model="haiku", unattended_write_requires_approval=True,
    )


class FakeKb:
    """Just enough of MemTraceClient to see what the harness asks it to do."""

    def __init__(self, nodes: dict | None = None, fail_on: str | None = None) -> None:
        self.nodes = nodes if nodes is not None else {
            "mem_task": {"title": "做 X", "body": '{"checkpoint": "build"}', "tags": ["task", "status:open", "p"]},
        }
        self.created: list[dict] = []
        self.edges: list[tuple] = []
        self.updates: list[dict] = []
        self.fail_on = fail_on

    def get_node(self, *, workspace_id, node_id, **kw):
        if node_id not in self.nodes:
            raise RuntimeError(f"no such node {node_id}")
        return dict(self.nodes[node_id])

    def create_node(self, **kw):
        if self.fail_on == kw["title"]:
            raise RuntimeError("create failed")
        node_id = f"mem_new{len(self.created)}"
        self.created.append(kw)
        self.nodes[node_id] = {"title": kw["title"], "body": kw["body"], "tags": list(kw["tags"])}
        return node_id

    def create_edge(self, **kw):
        self.edges.append((kw["from_id"], kw["to_id"], kw["relation"]))
        return True

    def update_node(self, **kw):
        self.updates.append(kw)
        if "tags" in kw and kw["tags"] is not None:
            self.nodes[kw["node_id"]]["tags"] = kw["tags"]


def claim_op(**overrides):
    op = {"op": "claim_done", "node_id": "mem_task", "title": "", "body": "已完成並通過測試",
          "content_type": "factual", "links": []}
    op.update(overrides)
    return op


def note_op(**overrides):
    op = {"op": "note", "node_id": "", "title": "遷移決定", "body": "不遷移舊資料，因為沒有讀取端依賴",
          "content_type": "procedural", "links": [{"to_id": "mem_task", "relation": "extends"}]}
    op.update(overrides)
    return op


class NormalizeTests(TestCase):
    def test_usable_proposals_survive_and_malformed_ones_are_dropped_individually(self) -> None:
        ops = normalize_kb_updates([
            claim_op(),
            claim_op(node_id="not_a_node"),          # invented id
            claim_op(body="  "),                     # nothing claimed
            note_op(title=""),                       # no title
            {"op": "delete_everything", "body": "x"},
            "garbage",
            note_op(links=[{"to_id": "mem_task", "relation": "extends"}, {"to_id": "x", "relation": "extends"},
                           {"to_id": "mem_task", "relation": "owns"}]),
        ])
        self.assertEqual([o["op"] for o in ops], ["claim_done", "note"])
        self.assertEqual(ops[1]["links"], [{"to_id": "mem_task", "relation": "extends"}])

    def test_nothing_or_non_list_is_no_updates(self) -> None:
        for raw in (None, "x", {}, []):
            self.assertEqual(normalize_kb_updates(raw), [])

    def test_at_most_three_proposals_are_considered(self) -> None:
        self.assertEqual(len(normalize_kb_updates([claim_op()] * 5)), 3)

    def test_the_controller_schema_carries_exactly_this_contract(self) -> None:
        schema = json.loads(output_schema_path("controller").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["kb_updates"], KB_UPDATES_SCHEMA)
        self.assertIn("kb_updates", schema["required"])


class ApplyTests(TestCase):
    def _apply(self, kb, ops):
        return apply_kb_updates(
            kb, workspace_id="ws", ops=normalize_kb_updates(ops), claim_id="clm_1",
            conversation_id="chat_1", claim_summary="做完 X",
        )

    def test_a_claim_is_a_separate_node_and_never_edits_the_original_body(self) -> None:
        kb = FakeKb()
        result = self._apply(kb, [claim_op()])
        [created] = kb.created
        self.assertIn("clm_1", created["body"])
        self.assertEqual(kb.edges, [("mem_new0", "mem_task", "extends")])
        [update] = kb.updates
        self.assertNotIn("body", update)
        self.assertEqual(update["node_id"], "mem_task")
        self.assertEqual(update["tags"], ["task", "p", STATUS_CLAIMED])
        self.assertEqual((result["node_id"], result["claim_node_id"]), ("mem_task", "mem_new0"))

    def test_controller_writes_are_not_labelled_drafts(self) -> None:
        kb = FakeKb()
        self._apply(kb, [claim_op(), note_op()])
        for created in kb.created:
            self.assertNotIn("draft", created["tags"])
            self.assertNotIn("human-gate", created["tags"])
            self.assertIn("controller", created["tags"])

    def test_a_note_is_recorded_and_linked(self) -> None:
        kb = FakeKb()
        result = self._apply(kb, [note_op()])
        [created] = kb.created
        self.assertEqual((created["title"], created["content_type"]), ("遷移決定", "procedural"))
        self.assertIn("對話 chat_1", created["body"])
        self.assertEqual(kb.edges, [("mem_new0", "mem_task", "extends")])
        self.assertIn("已記下知識", result["lines"][0])

    def test_tags_that_cannot_be_read_are_never_overwritten(self) -> None:
        kb = FakeKb({"mem_task": {"title": "做 X", "body": "b"}})
        result = self._apply(kb, [claim_op()])
        self.assertEqual(kb.updates, [])
        self.assertIn("標籤讀不到", result["lines"][0])

    def test_a_failing_proposal_is_reported_and_does_not_stop_the_others(self) -> None:
        kb = FakeKb(fail_on="遷移決定")
        result = self._apply(kb, [note_op(), claim_op(node_id="mem_missing"), claim_op()])
        self.assertEqual(len(result["lines"]), 3)
        self.assertIn("沒有完成", result["lines"][0])
        self.assertIn("no such node mem_missing", result["lines"][1])
        self.assertEqual(result["node_id"], "mem_task")

    def test_accept_and_reject_move_the_node_and_its_claim_node(self) -> None:
        kb = FakeKb()
        result = self._apply(kb, [claim_op()])
        claim = {"workspace_id": "ws", "node_id": "mem_task", "claim_node_id": result["claim_node_id"],
                 "conversation_id": "chat_1"}
        self.assertIsNone(accept_claim(kb, claim))
        self.assertIn(STATUS_DONE, kb.nodes["mem_task"]["tags"])
        self.assertNotIn(STATUS_CLAIMED, kb.nodes["mem_task"]["tags"])
        self.assertEqual(kb.updates[-2]["resolution_status"], "resolved")
        self.assertIn("status:accepted", kb.nodes[result["claim_node_id"]]["tags"])

        self.assertIsNone(reject_claim(kb, claim))
        self.assertIn(STATUS_OPEN, kb.nodes["mem_task"]["tags"])
        self.assertNotIn(STATUS_DONE, kb.nodes["mem_task"]["tags"])
        self.assertEqual(kb.updates[-2]["resolution_status"], "open")
        self.assertIn("status:rejected", kb.nodes[result["claim_node_id"]]["tags"])

    def test_a_knowledge_base_failure_while_resolving_is_reported_not_raised(self) -> None:
        kb = FakeKb()
        problem = accept_claim(kb, {"workspace_id": "ws", "node_id": "mem_gone", "claim_node_id": None})
        self.assertIn("no such node", problem)


def converge_stage(action="finish", **extra):
    return SimpleNamespace(stage="converge", state="succeeded", artifact={"action": action, "reason": "done", **extra})


def run_summary(*, status="succeeded", stages=None, workspace="ws_test"):
    return SimpleNamespace(
        status=status, recommendation="X 已完成", conversation_id="chat_1",
        task=SimpleNamespace(workspace_id=workspace),
        stages=stages if stages is not None else [
            SimpleNamespace(stage="plan", state="succeeded", artifact={}),
            converge_stage(kb_updates=[claim_op()]),
        ],
    )


class GatewayClaimTests(TestCase):
    def _gateway(self, kb=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.store = TraceStore(tmp / "t.sqlite3")
        scope = ProjectScope(
            name="test_proj", workspace_id="ws_test", working_directory=tmp,
            scope_file_path=tmp / "harness-scope.md",
        )
        self.mgr = PrimarySessionManager(self.store)
        self.kb = kb if kb is not None else FakeKb()
        gateway = TelegramGateway(
            _config(tmp), ApprovalManager(self.store, {12345}), ChatTriage([scope]), self.mgr, [scope],
            memtrace_client=self.kb,
        )
        gateway.send_message = MagicMock()
        gateway.send_message_with_keyboard = MagicMock(return_value=5)
        gateway.clear_message_keyboard = MagicMock()
        gateway.answer_callback_query = MagicMock()
        return gateway

    def _tap(self, gateway, data):
        return gateway._handle_callback_query(
            {"id": "cb", "data": data, "message": {"chat": {"id": 12345}, "message_id": 9}}
        )

    def _file(self, gateway, summary=None):
        gateway._report_run_outcome(conv_id="chat_1", summary=summary or run_summary(), final_text="✅ 完成", chat_id=12345)
        [call] = gateway.send_message_with_keyboard.call_args_list
        text, keyboard = call.args[1], call.args[2]
        claim_id = keyboard[0][0]["callback_data"].split(":")[1]
        return claim_id, text

    def test_a_finished_run_is_reported_as_awaiting_acceptance_with_the_kb_update_applied(self) -> None:
        gateway = self._gateway()
        claim_id, text = self._file(gateway)
        self.assertIn("請驗收", text)
        self.assertIn("沒有期限", text)
        self.assertIn("完成宣告", text)
        claim = self.store.get_completion_claim(claim_id)
        self.assertEqual((claim["status"], claim["node_id"], claim["project"]), ("claimed", "mem_task", "test_proj"))
        self.assertIn(STATUS_CLAIMED, self.kb.nodes["mem_task"]["tags"])
        gateway.send_message.assert_not_called()

    def test_accepting_marks_the_node_done_and_records_a_followed_decision(self) -> None:
        gateway = self._gateway()
        claim_id, _ = self._file(gateway)
        self._tap(gateway, f"claim_ok:{claim_id}")

        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "accepted")
        self.assertIn(STATUS_DONE, self.kb.nodes["mem_task"]["tags"])
        [rec] = self.store.list_decision_records("test_proj", kind="task_claim")
        self.assertEqual((rec["outcome"], rec["followed"], rec["subject"]), ("claim_accepted", True, claim_id))
        turn = [t for t in self.store.get_primary_session_turns("psess_test_proj") if t["turn_type"] == "decision"][0]
        self.assertEqual(turn["speaker"], "user")
        self.assertIn("驗收通過", turn["content"])

    def test_sending_it_back_reopens_the_node_and_becomes_a_warning_in_the_controllers_precedent(self) -> None:
        gateway = self._gateway()
        claim_id, _ = self._file(gateway)
        self._tap(gateway, f"claim_no:{claim_id}")

        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "rejected")
        self.assertIn(STATUS_OPEN, self.kb.nodes["mem_task"]["tags"])
        [rec] = self.store.list_decision_records("test_proj", kind="task_claim")
        self.assertEqual((rec["outcome"], rec["followed"]), ("claim_rejected", False))
        block = precedent_block(self.store, "test_proj")
        self.assertIn("task_claim", block)
        self.assertIn("⚠ Controller 宣告完成，被你退回", block)
        self.assertIn("X 已完成", block)

    def test_a_claim_is_answered_once_and_silence_accepts_nothing(self) -> None:
        gateway = self._gateway()
        claim_id, _ = self._file(gateway)
        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "claimed")
        self._tap(gateway, f"claim_ok:{claim_id}")
        self.assertEqual(self._tap(gateway, f"claim_no:{claim_id}"), "這項已經處理過了")
        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "accepted")
        self.assertEqual(len(self.store.list_decision_records("test_proj", kind="task_claim")), 1)

    def test_a_run_that_did_not_finish_through_converge_files_no_claim(self) -> None:
        gateway = self._gateway()
        for summary in (
            run_summary(status="failed"),
            run_summary(status="needs_human"),
            run_summary(stages=[SimpleNamespace(stage="operate", state="succeeded", artifact={})]),
            run_summary(stages=[converge_stage("ask_human")]),
            run_summary(workspace="ws_someone_else"),
        ):
            gateway._report_run_outcome(conv_id="chat_1", summary=summary, final_text="結果", chat_id=12345)
        gateway.send_message_with_keyboard.assert_not_called()
        self.assertEqual(gateway.send_message.call_count, 5)

    def test_acceptance_works_even_when_the_controller_proposed_no_update(self) -> None:
        gateway = self._gateway()
        summary = run_summary(stages=[converge_stage(kb_updates=None)])
        claim_id, text = self._file(gateway, summary)
        self.assertNotIn("🧠", text)
        self.assertEqual(self.kb.created, [])
        self._tap(gateway, f"claim_ok:{claim_id}")
        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "accepted")

    def test_a_broken_knowledge_base_never_blocks_the_report(self) -> None:
        kb = FakeKb(nodes={})  # the proposed node does not exist
        gateway = self._gateway(kb)
        claim_id, text = self._file(gateway)
        self.assertIn("知識庫更新沒有完成", text)
        self.assertEqual(self.store.get_completion_claim(claim_id)["status"], "claimed")
