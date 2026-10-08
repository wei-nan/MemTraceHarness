from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import cli
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.decision_records import record_resolution
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.work_review import (
    MAX_PROPOSALS,
    HARNESS_FACTS,
    ReviewOutcome,
    collect_report,
    describe_review,
    review_due,
    run_work_review,
    validate_review,
    verify_report_evidence,
    write_lessons,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def seed(ts: TraceStore, *, runs, workspace="ws_a", start=NOW - timedelta(days=3)):
    """runs: list of (status, goal) — one loop run each, with stages and model calls."""
    with ts._connection() as conn:
        for i, (status, goal) in enumerate(runs):
            run_id = f"run_{workspace}_{start.timestamp():.0f}_{i}"
            created = (start + timedelta(minutes=i)).isoformat()
            conv = f"chat_{i:04d}"
            conn.execute(
                "INSERT INTO runs (id, workspace_id, goal, task_json, summary_json, conversation_id, created_at) "
                "VALUES (?, ?, ?, '{}', ?, ?, ?)",
                (run_id, workspace, goal, json.dumps({"status": status, "recommendation": f"結果 {status}"}), conv, created),
            )
            conn.execute(
                "INSERT INTO loop_stages (run_id, sequence, stage, profile_id, state, artifact_json) "
                "VALUES (?, 1, 'control-start', 'controller', 'succeeded', ?)",
                (run_id, json.dumps({"action": "run_operational_action"})),
            )
            quota = i % 2 == 0
            conn.execute(
                "INSERT INTO cli_executions (run_id, adapter_id, provider, status, usage_json, started_at, completed_at, "
                "duration_ms, profile_id, fallback_index, failure_category) "
                "VALUES (?, 'controller', 'codex', ?, '{}', ?, ?, 9000, 'controller', 0, ?)",
                (run_id, "failed" if quota else "succeeded", created, created, "quota_exhausted" if quota else "none"),
            )
            conn.execute(
                "INSERT INTO cli_executions (run_id, adapter_id, provider, status, usage_json, started_at, completed_at, "
                "duration_ms, profile_id, fallback_index, failure_category) "
                "VALUES (?, 'controller', 'claude', 'succeeded', '{}', ?, ?, 11000, 'controller', 1, 'none')",
                (run_id, created, created),
            ) if quota else None


def store(tmp):
    return TraceStore(Path(tmp) / "t.sqlite3")


def report_of(ts, **kw):
    return collect_report(
        ts, workspace_id="ws_a", project="P", project_names=["P"], since=kw.get("since"),
        until=kw.get("until", NOW.isoformat()),
    )


class ReportTests(TestCase):
    def test_the_report_is_computed_from_the_records_with_exact_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "查價")] * 6 + [("needs_human", "401 的排程")] * 3 + [("failed", "壞掉")])
            r = report_of(ts)
            self.assertEqual(r.runs, 10)
            self.assertIn("成功 6、需要人 3（30%）、失敗 1（10%）", r.text)
            self.assertIn("run_operational_action 10", r.text)
            self.assertIn("controller / codex：共 10 次，額度用盡 5（50%）", r.text)
            self.assertIn("改由備援模型接手的呼叫：controller 的 claude 備援呼叫 5 次", r.text)
            self.assertIn("chat_0009（", r.text)                   # the failed run is listed as a case
            self.assertIn("需要人", r.text)

    def test_only_runs_inside_the_period_and_workspace_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "舊的")] * 4, start=NOW - timedelta(days=19))
            seed(ts, runs=[("failed", "新的")] * 2, start=NOW - timedelta(days=2))
            seed(ts, runs=[("failed", "別的專案")] * 7, workspace="ws_other", start=NOW - timedelta(days=2))
            since = (NOW - timedelta(days=10)).isoformat()
            r = report_of(ts, since=since)
            self.assertEqual(r.runs, 2)
            self.assertIn("失敗 2（100%）", r.text)
            self.assertIn("上一期（同樣長度）：共 4 次", r.text)       # the 4 older runs fall in the previous period
            self.assertEqual(report_of(ts).runs, 6)

    def test_repeated_questions_carry_the_date_of_the_last_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("needs_human", "反覆被問")])
            for _ in range(3):
                ts.create_approval_request(
                    conversation_id="chat_0000", workspace="ws_a", working_directory="/x",
                    reason="ambiguous_requirement", proposed_action="?",
                )
            text = report_of(ts).text
            self.assertIn("同一個對話被問了 3 次：chat_0000，最後一次在 20", text)

    def test_what_the_operator_decided_and_what_the_controller_held_back_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "x")])
            ts.add_decision_record(project="P", kind="task_claim", situation="s", outcome="claim_rejected", followed=False)
            ts.add_decision_record(project="P", kind="task_claim", situation="s", outcome="claim_accepted", followed=True)
            text = report_of(ts, until=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()).text
            self.assertIn("task_claim 共 2 筆（沒照建議或被退回 1）", text)


class ValidationTests(TestCase):
    report_text = (
        "# 工作報告\n執行結果：成功 220、需要人 53（17%）、失敗 41（13%）。\n"
        "controller / codex：共 315 次，額度用盡 87（28%），成功時平均 9 秒。\n"
    )
    report = type("R", (), {"text": report_text})()
    Q1 = "controller / codex：共 315 次，額度用盡 87（28%）"
    Q2 = "成功 220、需要人 53（17%）、失敗 41（13%）"

    def card(self, **kw):
        c = {"situation": "額度用盡 87 次，要怎麼處理？",
             "options": [{"label": "維持", "action": "照現狀", "tradeoff": "沒改動"},
                         {"label": "改順序", "action": "把 Claude 排前面", "tradeoff": "用掉另一邊額度"}],
             "recommended": 0, "reason": "28% 還可接受", "default_if_silent": "維持現狀"}
        c.update(kw)
        return c

    def validate(self, **reply):
        return validate_review(reply, self.report)

    def test_findings_must_quote_the_report_and_use_only_its_numbers(self) -> None:
        checked, dropped = self.validate(findings=[
            {"title": "額度壓力", "kind": "problem", "statement": "Codex 有 87 次額度用盡，佔 28%。", "evidence": [{"quote": self.Q1}]},
            {"title": "編造", "kind": "problem", "statement": "Codex 有 87 次額度用盡。", "evidence": [{"quote": "報告裡完全沒有這句話的內容喔喔喔"}]},
            {"title": "算錯", "kind": "problem", "statement": "額度用盡 92 次。", "evidence": [{"quote": self.Q1}]},
            {"title": "沒引文", "kind": "problem", "statement": "x", "evidence": []},
            {"title": "亂寫類型", "kind": "great", "statement": "需要人 53 次。", "evidence": [{"quote": self.Q2}]},
        ])
        self.assertEqual([f["title"] for f in checked["findings"]], ["額度壓力", "亂寫類型"])
        self.assertEqual(checked["findings"][1]["kind"], "unclear")             # an unknown kind is never trusted
        self.assertEqual(len(dropped), 3)
        self.assertTrue(any("數字 92" in d for d in dropped))

    def test_proposals_need_a_well_formed_card_evidence_and_supported_numbers(self) -> None:
        checked, dropped = self.validate(proposals=[
            {"title": "改順序", "evidence": [{"quote": self.Q1}], "decision_card": self.card()},
            {"title": "只有一個選項", "evidence": [{"quote": self.Q1}], "decision_card": self.card(options=[self.card()["options"][0]])},
            {"title": "數字不對", "evidence": [{"quote": self.Q1}], "decision_card": self.card(situation="額度用盡 99 次")},
            {"title": "沒引文", "evidence": [], "decision_card": self.card()},
        ])
        [proposal] = checked["proposals"]
        self.assertEqual(proposal["title"], "改順序")
        self.assertEqual(proposal["card"]["basis"], [])
        self.assertEqual(len(dropped), 3)

    def test_at_most_a_bounded_number_of_proposals_and_lessons_need_substance(self) -> None:
        many = [{"title": f"建議 {i}", "evidence": [{"quote": self.Q1}], "decision_card": self.card()} for i in range(6)]
        checked, _ = self.validate(
            proposals=many,
            lessons=[{"title": "太短", "body": "短", "evidence": [{"quote": self.Q1}]},
                     {"title": "有內容", "body": "Codex 額度用盡佔 28%，Controller 應預期備援。" * 2, "evidence": [{"quote": self.Q1}]}],
        )
        self.assertEqual(len(checked["proposals"]), MAX_PROPOSALS)
        self.assertEqual([l["title"] for l in checked["lessons"]], ["有內容"])

    def test_quotes_survive_rewrapping_but_not_changed_words(self) -> None:
        self.assertEqual(len(verify_report_evidence([{"quote": "成功 220、需要人 53（17%）、\n失敗 41（13%）"}], self.report_text)), 1)
        self.assertEqual(verify_report_evidence([{"quote": "成功 221、需要人 53（17%）、失敗 41（13%）"}], self.report_text), [])

    def test_the_prompt_tells_the_model_what_the_report_cannot(self) -> None:
        self.assertIn("quota_cooldown", HARNESS_FACTS)
        self.assertIn("NOT invoked", HARNESS_FACTS)
        self.assertIn("deliberate", HARNESS_FACTS)
        self.assertIn("2026-10-07", HARNESS_FACTS)


def reply_json(report_quote="成功 6、需要人 3（30%）、失敗 1（10%）"):
    card = {"situation": "需要人佔 3 次，要怎麼辦？",
            "options": [{"label": "維持", "action": "照現狀", "tradeoff": "沒改動"},
                        {"label": "調整", "action": "改提示", "tradeoff": "要維護"}],
            "recommended": 0, "reason": "先觀察", "default_if_silent": "維持現狀"}
    return json.dumps({
        "findings": [{"title": "需要人偏多", "kind": "problem", "statement": "10 次裡有 3 次需要人。",
                      "evidence": [{"quote": report_quote}]}],
        "proposals": [{"title": "調整提示", "evidence": [{"quote": report_quote}], "decision_card": card}],
        "lessons": [{"title": "監控類請求常停在授權", "body": "需要人 3 次多半是授權問題，應先確認授權再排程。" * 2,
                     "evidence": [{"quote": report_quote}]}],
        "summary": "整體尚可",
    }, ensure_ascii=False)


class ReviewRunTests(TestCase):
    def _run(self, ts, raw, **kw):
        return run_work_review(
            trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
            caller=lambda prompt: raw, now=NOW, **kw,
        )

    def test_the_first_review_covers_everything_and_the_next_starts_where_it_ended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            first = self._run(ts, reply_json())
            self.assertIsNone(first.report.period_start)
            self.assertEqual((len(first.findings), len(first.proposals), len(first.lessons)), (1, 1, 1))
            self.assertEqual(ts.last_work_review("ws_a")["period_end"], NOW.isoformat())

            seed(ts, runs=[("failed", "d")] * 2, start=NOW + timedelta(hours=1))
            later = run_work_review(
                trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
                caller=lambda p: json.dumps({"findings": [], "summary": ""}), now=NOW + timedelta(days=1),
            )
            self.assertEqual(later.report.period_start, NOW.isoformat())
            self.assertEqual(later.report.runs, 2)

    def test_the_previous_findings_are_shown_to_the_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            self._run(ts, reply_json())
            seed(ts, runs=[("failed", "d")] * 2, start=NOW + timedelta(hours=1))
            prompts = []
            run_work_review(
                trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
                caller=lambda p: prompts.append(p) or "{}", now=NOW + timedelta(days=1),
            )
            self.assertIn("上一次復盤你的發現", prompts[0])
            self.assertIn("需要人偏多", prompts[0])

    def test_a_failed_model_call_leaves_the_period_to_be_reviewed_next_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6)
            self.assertIsNone(self._run(ts, "not json"))
            self.assertIsNone(ts.last_work_review("ws_a"))                    # nothing counted as reviewed
            self.assertEqual(ts.last_work_review("ws_a", ok_only=False)["status"], "failed")

    def test_too_few_runs_means_no_automatic_review_and_no_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 2)
            caller = MagicMock()
            self.assertIsNone(run_work_review(trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
                                              caller=caller, min_runs=5, now=NOW))
            caller.assert_not_called()

    def test_a_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            out = self._run(ts, reply_json(), dry_run=True)
            self.assertTrue(out.dry_run)
            self.assertIsNone(ts.last_work_review("ws_a", ok_only=False))
            self.assertIn("試跑", describe_review(out))

    def test_the_weekly_review_waits_for_a_first_one_then_a_week(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            self.assertFalse(review_due(ts, "ws_a", NOW))                      # the first one is asked for
            self._run(ts, reply_json())
            self.assertFalse(review_due(ts, "ws_a", NOW + timedelta(days=3)))
            self.assertTrue(review_due(ts, "ws_a", NOW + timedelta(days=8)))


class FakeKb:
    def __init__(self):
        self.created, self.updated = [], []

    def create_node(self, **kw):
        self.created.append(kw)
        return f"mem_l{len(self.created)}"

    def update_node(self, **kw):
        self.updated.append(kw)


class LessonTests(TestCase):
    def test_lessons_are_written_not_as_drafts_with_their_quotes_and_updated_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb()
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            out = run_work_review(trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
                                  caller=lambda p: reply_json(), now=NOW)
            self.assertEqual(write_lessons(kb, ts, out, "ws_mem"), ["監控類請求常停在授權"])
            [node] = kb.created
            self.assertEqual(node["workspace_id"], "ws_mem")
            self.assertNotIn("draft", node["tags"])
            self.assertEqual(node["tags"], ["harness", "controller", "work-lesson"])
            self.assertIn("「成功 6、需要人 3（30%）、失敗 1（10%）」", node["body"])
            self.assertIsNotNone(out.lesson_run_id)
            # Same lesson again: nothing new; a changed one: updated in place.
            self.assertEqual(write_lessons(kb, ts, out, "ws_mem"), [])
            out.lessons[0]["body"] += "補充一句新的說明文字。"
            self.assertEqual(write_lessons(kb, ts, out, "ws_mem"), ["監控類請求常停在授權"])
            self.assertEqual((len(kb.created), len(kb.updated)), (1, 1))

    def test_a_dry_run_or_no_lessons_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb()
            out = ReviewOutcome(None, "P", "ws_a", report_of(ts), dry_run=True)
            self.assertEqual(write_lessons(kb, ts, out, "ws_mem"), [])
            self.assertEqual(kb.created, [])


def _gateway(tmp):
    path = Path(tmp)
    ts = TraceStore(path / "t.sqlite3")
    config = HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=path / "t.sqlite3", trace_root=path,
        claude_command="claude", codex_command="codex", antigravity_command="agy",
        antigravity_output_mode="auto", cli_timeout_seconds=900, telegram_bot_token="fake",
        telegram_allowed_chat_ids={12345}, project_index_path=None, chat_provider="claude",
        chat_model="haiku", unattended_write_requires_approval=True,
    )
    scope = ProjectScope(name="P", workspace_id="ws_a", working_directory=path,
                         scope_file_path=path / "harness-scope.md")
    gw = TelegramGateway(config, ApprovalManager(ts, {12345}), ChatTriage([scope]), PrimarySessionManager(ts), [scope])
    gw.send_message = MagicMock()
    gw.send_message_with_keyboard = MagicMock(return_value=3)
    gw.clear_message_keyboard = MagicMock()
    gw.answer_callback_query = MagicMock()
    return gw, ts, scope


class GatewayTests(TestCase):
    def _make_outcome(self, ts):
        seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
        return run_work_review(trace_store=ts, workspace_id="ws_a", project="P", project_names=["P"],
                               caller=lambda p: reply_json(), now=NOW)

    def test_the_report_then_each_proposal_as_a_decision_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            out = self._make_outcome(ts)
            gw.notify_review_outcome(out, scope, 12345)
            self.assertIn("工作復盤", gw.send_message.call_args.args[1])
            self.assertIn("需要人偏多", gw.send_message.call_args.args[1])
            card_text, keyboard = gw.send_message_with_keyboard.call_args.args[1:3]
            self.assertIn("需要你決定", card_text)
            self.assertEqual([b[0]["callback_data"].split(":")[0] for b in keyboard], ["pick", "pick", "reject"])

    def test_answering_a_proposal_is_remembered_and_resumes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            out = self._make_outcome(ts)
            gw._resume_approved_conversation = MagicMock(wraps=gw._resume_approved_conversation)
            gw.notify_review_outcome(out, scope, 12345)
            [req] = ts.list_pending_approvals_for_workspace("ws_a")
            gw._handle_callback_query({"id": "cb", "data": f"pick:{req['id']}:1",
                                       "message": {"chat": {"id": 12345}, "message_id": 5}})
            [rec] = ts.list_decision_records("P", kind="review_proposal")
            self.assertEqual((rec["outcome"], rec["followed"]), ("picked_other", False))
            self.assertIn("harness 不會自己改動", gw.send_message.call_args.args[1])
            self.assertEqual(ts.get_approval_request(req["id"])["status"], "approved")
            # nothing was started: no task slot, no lock for the review's pseudo-conversation
            self.assertIsNone(ts.get_workspace_lock("ws_a"))

    def test_the_review_command_and_a_request_in_chat_both_start_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.work_review_now = MagicMock()
            gw.process_update({"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/review"}})
            gw.work_review_now.assert_called_once_with(scope, 12345)
            gw.work_review_now.reset_mock()
            self.assertIn("開始復盤", gw._start_work_review(scope, 12345))
            gw.work_review_now.assert_called_once_with(scope, 12345)
            gw.work_review_now = None
            self.assertIn("沒有啟用", gw._start_work_review(scope, 12345))
            self.assertIn("請指定專案", gw._start_work_review(None, 12345))

    def test_the_chat_marker_starts_a_review_and_is_hidden_from_the_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.work_review_now = MagicMock()
            raw = "好，我來整理一份有數據的復盤。\nHARNESS_REVIEW_START::回顧最近的任務哪裡卡住"
            text, payload = gw._extract_marker_line(raw, gw._REVIEW_START_MARKER)
            self.assertEqual(text, "好，我來整理一份有數據的復盤。")
            self.assertEqual(payload, "回顧最近的任務哪裡卡住")
            self.assertIn("HARNESS_REVIEW_START::", gw._REVIEW_START_MARKER)


class CliTests(TestCase):
    def test_a_review_is_run_reported_and_its_lessons_written_one_per_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            seed(ts, runs=[("succeeded", "a")] * 6 + [("needs_human", "b")] * 3 + [("failed", "c")])
            kb = FakeKb()
            gw.notify_review_outcome = MagicMock()
            with patch.object(cli, "make_review_caller", return_value=lambda prompt: reply_json()), patch.object(
                gw.config.__class__, "memory_workspace_id_for", lambda self, name, ws: "ws_mem"
            ):
                ran = cli.run_work_reviews(gw.config, ts, kb, [scope, scope], {"P": gw}, chat_id=12345, force=True)
            self.assertEqual(ran, 1)                                           # the same workspace twice: one review
            gw.notify_review_outcome.assert_called_once()
            self.assertEqual(len(kb.created), 1)

    def test_the_weekly_pass_skips_a_workspace_that_has_never_been_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            seed(ts, runs=[("succeeded", "a")] * 8)
            with patch.object(cli, "make_review_caller", return_value=lambda prompt: reply_json()):
                self.assertEqual(cli.run_work_reviews(gw.config, ts, None, [scope], {"P": gw}), 0)
