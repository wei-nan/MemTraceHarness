from __future__ import annotations

import json
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.primary_session import SCHEDULE_REPORT, PrimarySessionManager
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.trigger_review import (
    MAX_CONSECUTIVE_SILENT,
    MAX_SCANNER_DEFERRALS,
    decide_scanner_verdicts,
    decide_schedule_delivery,
    history_entry,
    silent_tag,
)


def reply(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False)


def pushed(n: int) -> list[dict]:
    return [history_entry(f"report {i}") for i in range(n)]


def held(n: int) -> list[dict]:
    return [history_entry(silent_tag("no change") + f"report {i}") for i in range(n)]


class ScheduleDeliveryTests(TestCase):
    def _decide(self, raw, *, status="succeeded", history=None):
        caller = MagicMock(side_effect=raw if isinstance(raw, Exception) else None, return_value=raw)
        history = pushed(3) if history is None else history
        return decide_schedule_delivery(caller=caller, status=status, history=history, prompt="p"), caller

    def test_a_repeat_that_cites_what_it_repeats_is_held_for_the_digest(self) -> None:
        delivery, _ = self._decide(reply(delivery="digest_only", reason="同上輪", compared_to=1))
        self.assertEqual((delivery.action, delivery.overridden), ("digest_only", None))

    def test_holding_back_without_citing_a_comparison_is_overruled(self) -> None:
        for compared in (None, 0, 4, "1", True):
            delivery, _ = self._decide(reply(delivery="digest_only", reason="同上輪", compared_to=compared))
            self.assertEqual((delivery.action, delivery.overridden), ("push", "no_comparison_cited"), compared)

    def test_holding_back_needs_a_reason(self) -> None:
        delivery, _ = self._decide(reply(delivery="digest_only", reason="", compared_to=1))
        self.assertEqual((delivery.action, delivery.overridden), ("push", "no_reason"))

    def test_errors_are_never_held_back_and_cost_no_model_call(self) -> None:
        for status in ("failed", "budget_exhausted"):
            delivery, caller = self._decide(reply(delivery="digest_only", reason="r", compared_to=1), status=status)
            self.assertEqual((delivery.action, delivery.overridden), ("push", "error"))
            caller.assert_not_called()

    def test_an_alert_may_only_be_held_back_if_it_repeats_one_the_operator_saw(self) -> None:
        verdict = reply(delivery="digest_only", reason="同一個警示", compared_to=1)
        seen, _ = self._decide(verdict, status="needs_human", history=pushed(2))
        self.assertEqual(seen.action, "digest_only")
        unseen, _ = self._decide(verdict, status="needs_human", history=held(2))
        self.assertEqual((unseen.action, unseen.overridden), ("push", "repeats_a_report_the_operator_never_saw"))

    def test_a_schedule_is_paused_only_with_enough_history_and_never_on_an_alert(self) -> None:
        verdict = reply(delivery="pause_and_notify", reason="部位已賣出，沒有東西可追蹤", compared_to=None)
        self.assertEqual(self._decide(verdict, history=pushed(3))[0].action, "pause_and_notify")
        self.assertEqual(self._decide(verdict, history=pushed(2))[0].overridden, "too_little_history_to_pause")
        self.assertEqual(self._decide(verdict, status="needs_human")[0].overridden, "alert_not_paused")

    def test_a_long_silence_is_broken_by_a_push(self) -> None:
        delivery, caller = self._decide(
            reply(delivery="digest_only", reason="r", compared_to=1), history=held(MAX_CONSECUTIVE_SILENT)
        )
        self.assertEqual((delivery.action, delivery.overridden), ("push", "heartbeat"))
        caller.assert_not_called()

    def test_a_cited_report_must_be_one_the_prompt_showed(self) -> None:
        # Only the first five reports are shown to the model, so R6 cannot be cited.
        delivery, _ = self._decide(
            reply(delivery="digest_only", reason="r", compared_to=6), history=pushed(8)
        )
        self.assertEqual(delivery.overridden, "no_comparison_cited")

    def test_any_trouble_with_the_review_pushes_the_result(self) -> None:
        for raw in (RuntimeError("model down"), None, "not json", reply(delivery="shout", reason="x"), reply(x=1)):
            delivery, _ = self._decide(raw)
            self.assertEqual((delivery.action, delivery.overridden), ("push", "review_unavailable"), raw)

    def test_a_push_verdict_passes_straight_through(self) -> None:
        delivery, _ = self._decide(reply(delivery="push", reason="停損價已觸及", compared_to=None))
        self.assertEqual((delivery.action, delivery.overridden), ("push", None))


class ScannerVerdictTests(TestCase):
    candidates = [{"id": "gh:r#1", "title": "a"}, {"id": "gh:r#2", "title": "b"}]
    precedent = "- D4 · 2026-10-05 · unattended_write · x → 拒絕"

    def _verdicts(self, verdicts, deferrals=None):
        return decide_scanner_verdicts(
            caller=lambda prompt: reply(verdicts=verdicts),
            candidates=self.candidates, prompt="p", precedent=self.precedent,
            deferrals=deferrals or {},
        )

    def test_drop_needs_evidence_the_model_was_actually_shown(self) -> None:
        got = self._verdicts([
            {"id": "gh:r#1", "verdict": "drop", "reason": "已決定不做", "evidence": []},
            {"id": "gh:r#2", "verdict": "drop", "reason": "已決定不做", "evidence": ["D99"]},
        ])
        self.assertEqual(got, {})
        ok = self._verdicts([{"id": "gh:r#1", "verdict": "drop", "reason": "已決定不做", "evidence": ["D4", "D99"]}])
        self.assertEqual(ok["gh:r#1"].evidence, ("D4",))

    def test_a_candidate_cannot_be_deferred_forever(self) -> None:
        verdict = [{"id": "gh:r#1", "verdict": "defer", "reason": "等 #2", "evidence": []}]
        self.assertIn("gh:r#1", self._verdicts(verdict, {"gh:r#1": MAX_SCANNER_DEFERRALS - 1}))
        self.assertNotIn("gh:r#1", self._verdicts(verdict, {"gh:r#1": MAX_SCANNER_DEFERRALS}))

    def test_unknown_ids_and_unusable_replies_change_nothing(self) -> None:
        self.assertEqual(self._verdicts([{"id": "gh:r#9", "verdict": "propose", "reason": "x"}]), {})
        self.assertEqual(
            decide_scanner_verdicts(
                caller=lambda p: "garbage", candidates=self.candidates, prompt="p",
                precedent="", deferrals={},
            ),
            {},
        )

        def boom(prompt):
            raise RuntimeError("down")

        self.assertEqual(
            decide_scanner_verdicts(
                caller=boom, candidates=self.candidates, prompt="p", precedent="", deferrals={}
            ),
            {},
        )


def _config(tmp: Path) -> HarnessConfig:
    return HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=tmp / "t.sqlite3",
        trace_root=tmp, claude_command="claude", codex_command="codex",
        antigravity_command="agy", antigravity_output_mode="auto", cli_timeout_seconds=900,
        telegram_bot_token="fake", telegram_allowed_chat_ids={12345}, project_index_path=None,
        chat_provider="claude", chat_model="haiku", unattended_write_requires_approval=True,
    )


class ScannerReviewTests(TestCase):
    def _scan(self, review_reply, *, items=None, seed=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.store = TraceStore(tmp / "t.sqlite3")
        if seed is not None:
            seed(self.store)
        scope = ProjectScope(
            name="proj", workspace_id="ws_scan", working_directory=tmp,
            scope_file_path=tmp / "harness-scope.md",
        )
        body = '{"checkpoint": "build", "current_stage": "dev", "gate_state": {"latest_verdict": "PASS"}}'
        memtrace = MagicMock()
        memtrace.search_nodes.return_value = items or [
            {"id": "mem_a", "title": "舊的", "tags": ["task", "status:open"], "body": body,
             "created_at": "2026-09-01T00:00:00Z"},
            {"id": "mem_b", "title": "新的", "tags": ["task", "status:open"], "body": body,
             "created_at": "2026-09-02T00:00:00Z"},
        ]
        gateway = MagicMock()
        self.gateway = gateway
        scanner = UnattendedScanner(
            _config(tmp), self.store, [scope], memtrace, gateway=gateway,
            approval_mgr=ApprovalManager(self.store, {12345}),
            review_caller_factory=lambda project: (lambda prompt: review_reply),
        )
        return scanner, scanner.run_scan_pass()

    def _proposal(self):
        with self.store._connection() as conn:
            row = conn.execute(
                "SELECT proposed_action FROM approval_requests WHERE reason = 'unattended_write'"
            ).fetchone()
        return row[0] if row else None

    def test_the_proposal_carries_the_controllers_reason(self) -> None:
        _, res = self._scan(reply(verdicts=[{"id": "mem_a", "verdict": "propose", "reason": "改動小、沒有風險"}]))
        self.assertEqual(res["ws_scan"], "found_2_items")
        proposal = self._proposal()
        self.assertIn("舊的", proposal)
        self.assertIn("Controller 的看法：改動小、沒有風險", proposal)

    def test_a_deferred_candidate_is_skipped_and_recorded_not_asked(self) -> None:
        self._scan(reply(verdicts=[{"id": "mem_a", "verdict": "defer", "reason": "要等另一項"}]))
        proposal = self._proposal()
        self.assertIn("新的", proposal.split("佇列")[0])
        [record] = self.store.list_decision_records("proj", kind="scanner_candidate")
        self.assertEqual((record["outcome"], record["subject"]), ("controller_deferred", "mem_a"))

    def test_a_dropped_candidate_is_told_to_the_operator_and_not_offered_again(self) -> None:
        drop = reply(verdicts=[{"id": "mem_a", "verdict": "drop", "reason": "已決定不做", "evidence": ["D1"]}])
        scanner, _ = self._scan(
            drop,
            seed=lambda store: store.add_decision_record(
                project="proj", kind="unattended_write", situation="舊的", outcome="rejected", followed=False
            ),
        )
        self.gateway.notify_all_allowlisted.assert_any_call(
            "🗑 我判斷 backlog 項目「舊的」（mem_a）不再適用：已決定不做（依據 D1）。"
            "7 天內不會再提案；如果我判斷錯了，直接告訴我。"
        )
        self.assertIn("新的", self._proposal().split("佇列")[0])

        # A later pass, with no review at all, still does not offer it inside the cool-down.
        with self.store._connection() as conn:
            conn.execute("DELETE FROM approval_requests")
            conn.execute("DELETE FROM workspace_locks")
        scanner._review_caller_factory = None
        scanner.run_scan_pass()
        self.assertNotIn("舊的", self._proposal().split("佇列")[0])

    def test_a_drop_that_cites_nothing_the_model_was_shown_is_proposed_instead(self) -> None:
        self._scan(
            reply(verdicts=[{"id": "mem_a", "verdict": "drop", "reason": "已決定不做", "evidence": ["D1"]}])
        )
        self.assertIn("舊的", self._proposal().split("佇列")[0])
        self.gateway.notify_all_allowlisted.assert_not_called()

    def test_a_review_that_fails_leaves_the_proposal_as_it_was(self) -> None:
        self._scan("garbage")
        self.assertIn("舊的", self._proposal().split("佇列")[0])
        self.assertNotIn("Controller 的看法", self._proposal())


class ScheduleReviewWiringTests(TestCase):
    def _gateway(self, review_reply):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.store = TraceStore(tmp / "t.sqlite3")
        config = _config(tmp)
        scope = ProjectScope(
            name="test_proj", workspace_id="ws_test", working_directory=tmp,
            scope_file_path=tmp / "harness-scope.md",
        )
        self.mgr = PrimarySessionManager(self.store)
        gateway = TelegramGateway(
            config, ApprovalManager(self.store, {12345}), ChatTriage([scope]), self.mgr, [scope]
        )
        gateway.send_message = MagicMock()
        gateway.send_message_with_keyboard = MagicMock(return_value=1)
        gateway.clear_message_keyboard = MagicMock()
        gateway.answer_callback_query = MagicMock()
        if review_reply is not None:
            gateway.review_caller_factory = lambda project: (lambda prompt: review_reply)
        self.scope = scope
        self.schedule_id = self.store.create_schedule(
            project=scope.name, workspace_id=scope.workspace_id, goal="追蹤持股", kind="interval",
            interval_seconds=600, chat_id=12345,
            next_run_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        return gateway

    def _seed_reports(self, n: int) -> None:
        for i in range(n):
            self.mgr.record_turn(
                project="test_proj", speaker="work_session_report", turn_type=SCHEDULE_REPORT,
                content=f"排程 x 執行完成：第 {i} 輪，持股無變化", schedule_id=self.schedule_id,
            )

    def _run(self, gateway, *, status="succeeded", text="持股無變化"):
        summary = MagicMock(conversation_id="chat_x", status=status, recommendation=text, stages=[])
        with patch("memtrace_harness.telegram_gateway.load_role_profiles", return_value={}), patch(
            "memtrace_harness.telegram_gateway.build_role_adapter_candidates", return_value={}
        ), patch("memtrace_harness.telegram_gateway.AgentLoopRunner") as runner:
            runner.return_value.run.return_value = summary
            gateway._start_or_queue_task(self.scope, "追蹤持股", 12345, schedule_id=self.schedule_id)
            for t in threading.enumerate():
                if t.name.startswith("agent-loop-"):
                    t.join(timeout=5)

    def _report_turns(self):
        return [t for t in self.store.get_primary_session_turns("psess_test_proj") if t["turn_type"] == SCHEDULE_REPORT]

    def test_without_a_review_the_result_is_pushed_as_before(self) -> None:
        gateway = self._gateway(None)
        self._seed_reports(2)
        self._run(gateway)
        pushed_texts = [c.args[1] for c in gateway.send_message.call_args_list]
        self.assertTrue(any("任務已完成" in t for t in pushed_texts))
        self.assertFalse(self._report_turns()[-1]["content"].startswith("【未推送"))

    def test_a_repeat_is_not_pushed_and_is_logged_tagged_for_the_digest(self) -> None:
        gateway = self._gateway(reply(delivery="digest_only", reason="持股與上輪相同", compared_to=1))
        self._seed_reports(2)
        self._run(gateway)
        pushed_texts = [c.args[1] for c in gateway.send_message.call_args_list]
        self.assertFalse(any("任務已完成" in t for t in pushed_texts))
        last = self._report_turns()[-1]["content"]
        self.assertTrue(last.startswith("【未推送：持股與上輪相同】"))
        self.assertIn("持股無變化", last)

    def test_a_model_that_wants_to_hold_back_without_a_citation_is_overruled(self) -> None:
        gateway = self._gateway(reply(delivery="digest_only", reason="沒變", compared_to=None))
        self._seed_reports(2)
        self._run(gateway)
        pushed_texts = [c.args[1] for c in gateway.send_message.call_args_list]
        self.assertTrue(any("任務已完成" in t for t in pushed_texts))

    def test_pausing_tells_the_operator_and_resuming_is_remembered_as_disagreement(self) -> None:
        gateway = self._gateway(reply(delivery="pause_and_notify", reason="部位已賣出", compared_to=None))
        self._seed_reports(3)
        self._run(gateway)

        row = self.store.get_schedule(self.schedule_id)
        self.assertIsNotNone(row["paused_until"])
        text, keyboard = gateway.send_message_with_keyboard.call_args.args[1:3]
        self.assertIn("部位已賣出", text)
        self.assertEqual(
            [b["callback_data"] for b in keyboard[0]],
            [f"sched_resume:{self.schedule_id}", f"sched_cancel:{self.schedule_id}"],
        )
        self.assertFalse(any("任務已完成" in c.args[1] for c in gateway.send_message.call_args_list))

        gateway._handle_callback_query(
            {"id": "cb", "data": f"sched_resume:{self.schedule_id}",
             "message": {"chat": {"id": 12345}, "message_id": 7}}
        )
        self.assertIsNone(self.store.get_schedule(self.schedule_id)["paused_until"])
        records = self.store.list_decision_records("test_proj", kind="schedule_pause")
        self.assertEqual(
            sorted((r["outcome"], r["followed"]) for r in records),
            [("controller_paused", None), ("resumed", False)],
        )
        # The Controller's own action is not shown as the operator's precedent; their reaction is.
        from memtrace_harness.decision_records import precedent_block

        block = precedent_block(self.store, "test_proj")
        self.assertIn("繼續排程", block)
        self.assertNotIn("controller_paused", block)
        self.assertNotIn("paused_by", block)
