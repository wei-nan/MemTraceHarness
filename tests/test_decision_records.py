from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.decision_card import normalize_card
from memtrace_harness.decision_records import (
    known_basis_ids,
    precedent_block,
    record_resolution,
    select_precedents,
)
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


def card():
    return normalize_card(
        {
            "situation": "舊資料要不要遷移？",
            "options": [
                {"label": "不遷移", "action": "只改新資料", "tradeoff": "格式不同"},
                {"label": "遷移", "action": "寫遷移腳本", "tradeoff": "需要備份"},
            ],
            "recommended": 0,
            "reason": "沒有讀取端依賴",
            "default_if_silent": "維持暫停",
        }
    )


class _Env:
    def __init__(self, tmp: str) -> None:
        self.tmp = tmp
        self.store = TraceStore(Path(tmp) / "t.sqlite3")
        self.approvals = ApprovalManager(self.store, {12345})

    def request(self, *, with_card=True, reason="ambiguous_requirement"):
        return self.approvals.request_approval(
            conversation_id="conv_1",
            workspace="ws_test",
            working_directory=self.tmp,
            reason=reason,
            proposed_action="要不要做這件事",
            decision_card=card() if with_card else None,
        )


class RecordResolutionTests(TestCase):
    def _record(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            req = env.request(with_card=kwargs.pop("with_card", True), reason=kwargs.pop("reason", "ambiguous_requirement"))
            req = env.approvals.get_request(req.id)
            result = record_resolution(env.store, req, project="p", **kwargs)
            return result, env.store.list_decision_records("p")

    def test_picking_the_recommended_option_is_recorded_as_followed(self) -> None:
        result, [rec] = self._record(action="clarify", chosen_index=0, answer="我選方案 A")
        self.assertEqual((rec["outcome"], rec["followed"], rec["chosen_text"]), ("picked_recommended", True, "A「不遷移」"))
        self.assertIn("【決策 D1】", result[1])
        self.assertNotIn("沒有照建議", result[1])

    def test_picking_another_option_is_a_deviation_and_the_log_turn_says_so(self) -> None:
        result, [rec] = self._record(action="clarify", chosen_index=1, answer="我選方案 B")
        self.assertEqual((rec["outcome"], rec["followed"]), ("picked_other", False))
        self.assertIn("建議是 A「不遷移」，我沒有照建議", result[1])

    def test_a_free_text_answer_is_a_deviation_but_adds_no_second_log_turn(self) -> None:
        result, [rec] = self._record(action="clarify", answer="都不要，先把 schema 凍結")
        self.assertEqual((rec["outcome"], rec["followed"]), ("free_text", False))
        self.assertEqual(rec["reason"], "都不要，先把 schema 凍結")
        self.assertIsNone(result)

    def test_abandoning_a_card_is_not_counted_as_disagreeing_with_it(self) -> None:
        _, [rec] = self._record(action="reject")
        self.assertEqual((rec["outcome"], rec["followed"]), ("abandoned", None))

    def test_a_cardless_proposal_follows_approve_and_rejects_on_reject(self) -> None:
        _, [approved] = self._record(action="approve", with_card=False, reason="unattended_write")
        self.assertEqual((approved["outcome"], approved["followed"]), ("approved", True))
        _, [rejected] = self._record(action="reject", with_card=False, reason="unattended_write")
        self.assertEqual((rejected["outcome"], rejected["followed"]), ("rejected", False))

    def test_technical_stops_are_not_recorded(self) -> None:
        for reason in ("model_output_invalid", "config_change_required", "budget_exhausted"):
            result, records = self._record(action="approve", with_card=False, reason=reason)
            self.assertIsNone(result)
            self.assertEqual(records, [])


class PrecedentBlockTests(TestCase):
    def _rec(self, id_, followed, text="x"):
        return {
            "id": id_, "created_at": "2026-10-0%dT00:00:00+00:00" % (id_ % 9 + 1), "kind": "k",
            "situation": f"s{id_}", "options": None, "recommended": None, "outcome": "approved",
            "chosen_text": text, "followed": followed, "reason": None,
        }

    def test_disagreements_are_listed_even_when_older_than_the_rest(self) -> None:
        newest_first = [self._rec(i, True) for i in range(20, 8, -1)]
        newest_first += [self._rec(3, False), self._rec(2, False)]
        picked = select_precedents(newest_first)
        ids = [r["id"] for r in picked]
        self.assertEqual(len(ids), 8)
        self.assertIn(3, ids)
        self.assertIn(2, ids)
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_block_lists_decisions_and_preferences_and_flags_deviations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _Env(tmp)
            req = env.approvals.get_request(env.request().id)
            record_resolution(env.store, req, project="p", action="clarify", chosen_index=1, answer="x")
            block = precedent_block(env.store, "p", preferences="溝通：\n- [#3] 簡短回報")
        self.assertIn("D1", block)
        self.assertIn("⚠ 不同於建議（A「不遷移」）", block)
        self.assertIn("[#3] 簡短回報", block)

    def test_nothing_to_say_is_an_empty_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(precedent_block(TraceStore(Path(tmp) / "t.sqlite3"), "p"), "")

    def test_known_basis_ids_cover_decisions_and_bracketed_preferences(self) -> None:
        self.assertEqual(known_basis_ids("- D12 · x\n- [#3] y\nD7"), {"D12", "D7", "P3"})


def _gateway(tmp: str):
    path = Path(tmp)
    store = TraceStore(path / "t.sqlite3")
    config = HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=path / "t.sqlite3",
        trace_root=path, claude_command="claude", codex_command="codex",
        antigravity_command="agy", antigravity_output_mode="auto", cli_timeout_seconds=900,
        telegram_bot_token="fake", telegram_allowed_chat_ids={12345}, project_index_path=None,
        chat_provider="claude", chat_model="haiku", unattended_write_requires_approval=True,
    )
    approvals = ApprovalManager(store, {12345})
    scope = ProjectScope(
        name="test_proj", workspace_id="ws_test", working_directory=path,
        scope_file_path=path / "harness-scope.md",
    )
    gateway = TelegramGateway(
        config, approvals, ChatTriage([scope]), PrimarySessionManager(store), [scope]
    )
    gateway.send_message = MagicMock()
    gateway.clear_message_keyboard = MagicMock()
    gateway.answer_callback_query = MagicMock()
    gateway._resume_approved_conversation = MagicMock()
    return gateway, approvals, store


class GatewayWiringTests(TestCase):
    def test_tapping_an_option_records_the_decision_and_logs_it_as_the_operators_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway, approvals, store = _gateway(tmp)
            req = approvals.request_approval(
                conversation_id="conv_1", workspace="ws_test", working_directory=tmp,
                reason="ambiguous_requirement", proposed_action="raw", decision_card=card(),
            )

            gateway._handle_callback_query(
                {"id": "cb", "data": f"pick:{req.id}:1", "message": {"chat": {"id": 12345}, "message_id": 9}}
            )

            [rec] = store.list_decision_records("test_proj")
            self.assertEqual((rec["outcome"], rec["followed"], rec["approval_id"]), ("picked_other", False, req.id))
            [turn] = store.get_primary_session_turns("psess_test_proj")
            self.assertEqual((turn["speaker"], turn["turn_type"]), ("user", "decision"))
            self.assertIn("【決策 D1】", turn["content"])
            gateway._resume_approved_conversation.assert_called_once()

    def test_project_context_gives_the_controller_precedent_as_its_own_item_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway, approvals, store = _gateway(tmp)
            store.add_decision_record(
                project="test_proj", kind="unattended_write", situation="做 #176？",
                outcome="rejected", followed=False,
            )
            items = gateway._project_context_items(gateway.projects[0])
            [precedent] = [i for i in items if i.content_type == "operator_precedent"]
            self.assertIn("D1", precedent.body)
            self.assertIn("拒絕了提案", precedent.body)

    def test_no_precedent_means_no_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gateway, _, _ = _gateway(tmp)
            items = gateway._project_context_items(gateway.projects[0])
            self.assertFalse([i for i in items if i.content_type == "operator_precedent"])


class RunRecordsStayLocalTests(TestCase):
    def test_a_runs_record_is_not_also_written_to_the_projects_workspace(self) -> None:
        # Every run used to leave a "Harness loop draft" node behind: a ten-minute schedule filled
        # one specification workspace with 285 near-identical nodes.
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            gateway, _, _ = _gateway(tmp)
            summary = MagicMock(conversation_id="chat_x", status="succeeded", recommendation="ok", stages=[])
            with patch("memtrace_harness.telegram_gateway.load_role_profiles", return_value={}), patch(
                "memtrace_harness.telegram_gateway.build_role_adapter_candidates", return_value={}
            ), patch("memtrace_harness.telegram_gateway.AgentLoopRunner") as runner:
                runner.return_value.run.return_value = summary
                gateway._run_new_task(gateway.projects[0], "chat_x", "追蹤持股", schedule_id="sched_a")
                gateway._run_new_task(gateway.projects[0], "chat_y", "請開發功能")
            writebacks = [c.kwargs["writeback"] for c in runner.return_value.run.call_args_list]
            self.assertEqual(writebacks, [False, False])
