from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from memtrace_harness.memory_digest import (
    DigestError,
    apply_chat_preference_correction,
    due_digest_dates,
    in_digest_window,
    ground_digest,
    preference_context_for,
    render_digests_context,
    resolve_preference,
    run_digest_for_date,
    sync_digests_to_memtrace,
    sync_operator_profile,
)
from memtrace_harness.trace_store import TraceStore

TZ = ZoneInfo("Asia/Taipei")
SESSION = "psess_proj"


def _add_turn(store: TraceStore, speaker: str, content: str, created_at: str) -> int:
    turn_id = store.append_primary_session_turn(
        primary_session_id=SESSION, project="proj", speaker=speaker, turn_type="chat", content=content
    )
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (created_at, turn_id))
    return turn_id


def _model(answer: dict):
    calls: list[str] = []

    def call(prompt: str):
        calls.append(prompt)
        return json.dumps(answer, ensure_ascii=False), "claude", "haiku"

    return call, calls


def _turns(*speakers: str) -> list[dict]:
    return [
        {"turn_seq": i + 1, "speaker": sp, "content": f"turn {i + 1}", "created_at": "2026-09-30T01:00:00+00:00"}
        for i, sp in enumerate(speakers)
    ]


class GroundingTests(TestCase):
    def test_items_without_valid_citations_are_discarded(self) -> None:
        digest, _, _ = ground_digest(
            {
                "summary": "s",
                "decisions": [
                    {"text": "kept", "turns": [1, 99]},
                    {"text": "no citation", "turns": []},
                    {"text": "only a turn from another day", "turns": [99]},
                    {"text": "", "turns": [1]},
                ],
            },
            digest_date="2026-09-30",
            turns=_turns("user", "assistant"),
            carried_open_items=[],
        )
        self.assertEqual(digest["decisions"], [{"text": "kept", "turns": [1]}])
        self.assertEqual(digest["discarded_ungrounded"], 3)

    def test_preference_citing_anything_but_the_humans_turns_is_discarded(self) -> None:
        # The bug that motivated this: an assistant's stock-analysis reply ended up
        # in the operator profile.
        _, prefs, _ = ground_digest(
            {
                "preference_candidates": [
                    {"text": "回覆一律用繁體中文", "turns": [1], "scope": "global", "category": "language_format", "explicit": True},
                    {"text": "16 檔個股評估", "turns": [2], "scope": "global", "category": "other", "explicit": False},
                    {"text": "mixed", "turns": [1, 2], "scope": "global", "category": "other", "explicit": False},
                    {"text": "odd category", "turns": [3], "scope": "nonsense", "category": "made_up", "explicit": "yes"},
                ]
            },
            digest_date="2026-09-30",
            turns=_turns("user", "assistant", "user"),
            carried_open_items=[],
        )
        self.assertEqual([p["text"] for p in prefs], ["回覆一律用繁體中文", "odd category"])
        self.assertEqual(prefs[1]["scope"], "global")
        self.assertEqual(prefs[1]["category"], "other")
        self.assertFalse(prefs[1]["explicit"])

    def test_open_items_carry_until_cited_as_resolved_or_too_old(self) -> None:
        carried = [
            {"text": "still open", "turns": [1], "since": "2026-09-29"},
            {"text": "resolved today", "turns": [1], "since": "2026-09-29"},
            {"text": "ancient", "turns": [1], "since": "2026-09-01"},
            {"text": "claimed resolved without evidence", "turns": [1], "since": "2026-09-29"},
        ]
        digest, _, _ = ground_digest(
            {
                "new_open_items": [{"text": "new", "turns": [2]}],
                "resolved_open_items": [{"index": 1, "turns": [2]}, {"index": 3, "turns": []}, {"index": 9, "turns": [1]}],
            },
            digest_date="2026-09-30",
            turns=_turns("user", "user"),
            carried_open_items=carried,
        )
        self.assertEqual(
            [i["text"] for i in digest["open_items"]],
            ["still open", "claimed resolved without evidence", "new"],
        )
        self.assertEqual(digest["open_items"][-1]["since"], "2026-09-30")
        self.assertEqual([i["text"] for i in digest["resolved_items"]], ["resolved today"])
        self.assertEqual(digest["resolved_items"][0]["resolved_turns"], [2])
        self.assertEqual([i["text"] for i in digest["expired_items"]], ["ancient"])
        self.assertEqual(digest["discarded_ungrounded"], 2)


class DigestRunTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_due_dates_use_local_days_and_skip_today_and_done_days(self) -> None:
        _add_turn(self.store, "user", "a", "2026-09-27T03:00:00+00:00")  # 09-27 local
        _add_turn(self.store, "user", "b", "2026-09-29T17:30:00+00:00")  # 09-30 01:30 local
        _add_turn(self.store, "user", "c", "2026-10-01T02:00:00+00:00")  # today local
        now = datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)

        self.assertEqual(
            due_digest_dates(self.store, "proj", SESSION, TZ, now, max_days_back=None),
            ["2026-09-27", "2026-09-30"],
        )
        self.assertEqual(
            due_digest_dates(self.store, "proj", SESSION, TZ, now, max_days_back=2),
            ["2026-09-30"],
        )
        self.store.save_memory_digest(
            project="proj", digest_date="2026-09-27", provider=None, model=None, turn_count=1, digest={}
        )
        self.assertEqual(
            due_digest_dates(self.store, "proj", SESSION, TZ, now, max_days_back=None),
            ["2026-09-30"],
        )

    def test_run_saves_digest_and_adopts_preferences_from_the_humans_own_words(self) -> None:
        _add_turn(self.store, "user", "以後回覆都用繁體中文", "2026-09-30T01:00:00+00:00")
        _add_turn(self.store, "assistant", "好的", "2026-09-30T01:01:00+00:00")
        call, calls = _model(
            {
                "summary": "討論語言",
                "decisions": [{"text": "回覆用繁中", "turns": [1, 2]}],
                "preference_candidates": [
                    {"text": "回覆一律用繁體中文", "turns": [1], "scope": "global", "category": "language_format", "explicit": True}
                ],
            }
        )

        outcome = run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call)

        self.assertEqual(len(outcome.preferences.adopted), 1)
        self.assertIn("#1 [user] 以後回覆都用繁體中文", calls[0])
        saved = self.store.get_memory_digest("proj", "2026-09-30")
        self.assertEqual(saved["digest"]["summary"], "討論語言")
        self.assertEqual(saved["digest"]["preferences_adopted"], 1)
        self.assertEqual(saved["provider"], "claude")
        [rule] = self.store.list_preference_rules(statuses=("adopted",))
        # Nothing waited for a click: it is in effect, attributed, and cites the human.
        self.assertEqual(rule["adopted_by"], "harness-digest")
        self.assertEqual(rule["evidence"][0]["quote"], "以後回覆都用繁體中文")
        self.assertIn("回覆一律用繁體中文", preference_context_for(self.store, "proj"))

        # Re-running the same day does not adopt the same rule twice.
        run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call)
        self.assertEqual(len(self.store.list_preference_rules(statuses=("adopted",))), 1)

    def test_next_day_sees_previous_open_items(self) -> None:
        _add_turn(self.store, "user", "要決定資料期間", "2026-09-29T01:00:00+00:00")
        _add_turn(self.store, "user", "資料期間定為三年", "2026-09-30T01:00:00+00:00")
        day1, _ = _model({"summary": "d1", "new_open_items": [{"text": "決定資料期間", "turns": [1]}]})
        run_digest_for_date(self.store, "proj", SESSION, "2026-09-29", TZ, day1)
        day2, calls = _model({"summary": "d2", "resolved_open_items": [{"index": 0, "turns": [2]}]})

        run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, day2)

        self.assertIn("[0] (since 2026-09-29) 決定資料期間", calls[0])
        digest = self.store.get_memory_digest("proj", "2026-09-30")["digest"]
        self.assertEqual(digest["open_items"], [])
        self.assertEqual(digest["resolved_items"][0]["text"], "決定資料期間")

    def test_unparseable_answer_raises_and_saves_nothing(self) -> None:
        _add_turn(self.store, "user", "hi", "2026-09-30T01:00:00+00:00")

        with self.assertRaises(DigestError):
            run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, lambda p: ("not json", "claude", "haiku"))
        self.assertIsNone(self.store.get_memory_digest("proj", "2026-09-30"))

    def test_sync_writes_unsynced_digests_and_rewrites_after_a_workspace_change(self) -> None:
        self.store.save_memory_digest(
            project="proj", digest_date="2026-09-30", provider=None, model=None, turn_count=1,
            digest={"summary": "s", "decisions": [{"text": "d", "turns": [1]}]},
        )
        client = MagicMock()
        client.create_node.side_effect = ["mem_a", "mem_b"]

        self.assertEqual(sync_digests_to_memtrace(self.store, client, "proj", "ws_shared"), 1)
        self.assertEqual(sync_digests_to_memtrace(self.store, client, "proj", "ws_shared"), 0)
        self.assertEqual(sync_digests_to_memtrace(self.store, client, "proj", "ws_dedicated"), 1)

        kwargs = client.create_node.call_args.kwargs
        self.assertEqual(kwargs["workspace_id"], "ws_dedicated")
        self.assertEqual(kwargs["title"], "Daily digest: proj 2026-09-30")
        self.assertIn("- d [turns 1]", kwargs["body"])
        self.assertEqual(self.store.get_memory_digest("proj", "2026-09-30")["memtrace_node_id"], "mem_b")


class PreferenceReviewTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _candidate(self, project: str, text: str, scope: str = "global") -> int:
        return self.store.add_preference_candidate(
            project=project, scope=scope, category="communication", text=text,
            evidence=[], explicit=True, source_digest_date="2026-09-30",
        )

    def test_adopt_with_edits_then_retire(self) -> None:
        rule_id = self._candidate("proj", "回覆簡短")

        rule = resolve_preference(self.store, rule_id, "adopt", text=" 回覆盡量簡短 ", scope="project")

        self.assertEqual((rule["status"], rule["text"], rule["scope"]), ("adopted", "回覆盡量簡短", "project"))
        self.assertIn("回覆盡量簡短", preference_context_for(self.store, "proj"))
        self.assertEqual(preference_context_for(self.store, "other"), "")
        with self.assertRaisesRegex(ValueError, "is adopted, cannot adopt"):
            resolve_preference(self.store, rule_id, "adopt")

        resolve_preference(self.store, rule_id, "retire")
        self.assertEqual(preference_context_for(self.store, "proj"), "")

    def test_global_rules_apply_to_every_project_and_dismissed_ones_to_none(self) -> None:
        resolve_preference(self.store, self._candidate("a", "用繁體中文"), "adopt")
        resolve_preference(self.store, self._candidate("a", "不要表格"), "dismiss")

        self.assertIn("用繁體中文", preference_context_for(self.store, "b"))
        self.assertNotIn("不要表格", preference_context_for(self.store, "a"))

    def test_rejects_bad_requests(self) -> None:
        rule_id = self._candidate("proj", "x")
        with self.assertRaises(ValueError):
            resolve_preference(self.store, rule_id, "approve")
        with self.assertRaises(ValueError):
            resolve_preference(self.store, rule_id, "adopt", text="   ")
        with self.assertRaises(ValueError):
            resolve_preference(self.store, rule_id, "adopt", scope="team")
        with self.assertRaisesRegex(ValueError, "not found"):
            resolve_preference(self.store, 999, "dismiss")

    def test_profile_sync_rewrites_the_node_instead_of_appending(self) -> None:
        resolve_preference(self.store, self._candidate("proj", "用繁體中文"), "adopt")
        client = MagicMock()
        client.search_nodes.return_value = [{"id": "mem_profile", "title": "Operator preference profile"}]

        sync_operator_profile(self.store, client, "ws_pref")

        client.get_node.assert_not_called()
        body = client.update_node.call_args.kwargs["body"]
        self.assertIn("- 用繁體中文", body)


class DigestContextTests(TestCase):
    def _row(self, day: str, summary: str, open_items: list[dict]) -> dict:
        return {"digest_date": day, "digest": {"summary": summary, "decisions": [], "open_items": open_items}}

    def test_open_items_come_from_the_newest_digest_only(self) -> None:
        text = render_digests_context(
            [
                self._row("2026-09-30", "newest", [{"text": "仍未完成", "since": "2026-09-29"}]),
                self._row("2026-09-29", "older", [{"text": "舊版未完成", "since": "2026-09-29"}]),
            ]
        )
        self.assertLess(text.index("2026-09-29"), text.index("2026-09-30"))
        self.assertIn("仍未完成", text)
        self.assertNotIn("舊版未完成", text)

    def test_oldest_days_are_dropped_first_when_over_budget(self) -> None:
        text = render_digests_context(
            [self._row("2026-09-30", "N" * 50, []), self._row("2026-09-29", "O" * 50, [])],
            char_budget=80,
        )
        self.assertIn("2026-09-30", text)
        self.assertNotIn("2026-09-29", text)


class DigestWindowTests(TestCase):
    def test_runs_only_during_the_two_oclock_hour_taipei_time(self) -> None:
        def at(hour: int, minute: int = 0) -> datetime:
            return datetime(2026, 10, 1, hour, minute, tzinfo=TZ)

        self.assertTrue(in_digest_window(at(2)))
        self.assertTrue(in_digest_window(at(2, 59)))
        self.assertFalse(in_digest_window(at(1, 59)))
        self.assertFalse(in_digest_window(at(3)))
        # An afternoon restart must not start digesting immediately.
        self.assertFalse(in_digest_window(at(17, 40)))


class ScheduleActivityInDigestTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sched(self, kind: str, schedule_id: str, content: str, at: str) -> None:
        turn_id = self.store.append_primary_session_turn(
            primary_session_id=SESSION, project="proj",
            speaker="system" if kind == "schedule_trigger" else "work_session_report",
            turn_type=kind, content=content, schedule_id=schedule_id,
        )
        with sqlite3.connect(self.store.db_path) as conn:
            conn.execute("UPDATE primary_sessions_hot_log SET created_at = ? WHERE id = ?", (at, turn_id))

    def test_only_each_schedules_last_result_is_shown_with_a_count(self) -> None:
        _add_turn(self.store, "user", "今天看盤", "2026-09-30T01:00:00+00:00")
        for i in range(5):
            at = f"2026-09-30T02:0{i}:00+00:00"
            self._sched("schedule_trigger", "sched_a", "排程 sched_a 觸發：追蹤持股", at)
            self._sched("schedule_report", "sched_a", f"排程 sched_a 執行完成：第 {i} 次", at)
        call, calls = _model({"summary": "看盤", "facts": [{"text": "最後一次未達門檻", "turns": [11]}]})

        outcome = run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call)

        prompt = calls[0]
        self.assertIn("sched_a「追蹤持股」：當天觸發 5 次，只列出最後一次的結果", prompt)
        self.assertIn("第 4 次", prompt)
        self.assertNotIn("第 3 次", prompt)
        self.assertNotIn("觸發：追蹤持股", prompt)  # trigger rows themselves are not log lines
        self.assertEqual(outcome.turn_count, 1)  # one real conversation turn
        # The last result can still be cited as evidence.
        self.assertEqual(outcome.digest["facts"][0]["turns"], [11])

    def test_a_day_with_only_unanswered_triggers_is_recorded_without_a_model_call(self) -> None:
        self._sched("schedule_trigger", "sched_a", "排程 sched_a 觸發：追蹤持股", "2026-09-30T02:00:00+00:00")
        carried = {"summary": "d0", "new_open_items": []}
        called = []

        outcome = run_digest_for_date(
            self.store, "proj", SESSION, "2026-09-30", TZ, lambda p: called.append(p) or ("{}", None, None)
        )

        self.assertEqual(called, [])
        self.assertEqual(outcome.turn_count, 0)
        self.assertIn("只有排程觸發", outcome.digest["summary"])
        self.assertIsNotNone(self.store.get_memory_digest("proj", "2026-09-30"))

    def test_the_prompt_warns_that_a_quoted_reply_is_not_the_humans_words(self) -> None:
        _add_turn(self.store, "user", "這檔呢\n（回覆的訊息：「建議入場」）", "2026-09-30T01:00:00+00:00")
        call, calls = _model({"summary": "s"})

        run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call)

        self.assertIn("never as evidence of what the human said", calls[0])


class AutonomousPreferenceTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rule(self, project: str, text: str, scope: str = "global") -> int:
        return self.store.add_preference_candidate(
            project=project, scope=scope, category="communication", text=text, evidence=[],
            explicit=True, source_digest_date="2026-09-29", status="adopted", adopted_by="harness-digest",
        )

    def _pref(self, text: str, turns: list[int], **extra) -> dict:
        return {"text": text, "turns": turns, "scope": "global", "category": "communication", "explicit": True, **extra}

    def _day(self, *contents: str) -> None:
        for i, content in enumerate(contents):
            _add_turn(self.store, "user", content, f"2026-09-30T01:0{i}:00+00:00")

    def _run(self, answer: dict):
        call, calls = _model(answer)
        return run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call), calls

    def test_the_prompt_shows_the_adopted_rules_this_project_can_see(self) -> None:
        mine = self._rule("proj", "回覆簡短")
        theirs = self._rule("other", "別專案的偏好", scope="project")
        self._day("你好")

        _, calls = self._run({"summary": "s"})

        self.assertIn(f"[{mine}] (global) 回覆簡短", calls[0])
        self.assertNotIn("別專案的偏好", calls[0])
        self.assertIsNotNone(theirs)

    def test_a_refinement_replaces_the_rule_it_names(self) -> None:
        old = self._rule("proj", "回覆簡短")
        self._day("回覆可以詳細一點，但先給結論")

        outcome, _ = self._run(
            {"summary": "s", "preference_candidates": [self._pref("先給結論，再詳細說明", [1], replaces=[old, 999])]}
        )

        [new] = outcome.preferences.adopted
        self.assertEqual([r["id"] for r in outcome.preferences.retired], [old])
        retired = self.store.get_preference_rule(old)
        self.assertEqual(retired["status"], "retired")
        self.assertIn(f"被 #{new['id']} 取代", retired["retire_reason"])

    def test_a_retirement_needs_a_real_adopted_rule_and_the_humans_own_turn(self) -> None:
        keep = self._rule("proj", "留著的規則")
        drop = self._rule("proj", "要撤回的規則")
        foreign = self._rule("other", "別人的專案規則", scope="project")
        _add_turn(self.store, "user", "那個『要撤回』的不用了", "2026-09-30T01:00:00+00:00")
        _add_turn(self.store, "assistant", "好，我不再套用", "2026-09-30T01:01:00+00:00")

        outcome, _ = self._run(
            {
                "summary": "s",
                "preference_retirements": [
                    {"id": drop, "turns": [1]},
                    {"id": keep, "turns": [2]},       # cites the assistant, not the human
                    {"id": foreign, "turns": [1]},    # not a rule this project can see
                    {"id": 424242, "turns": [1]},     # no such rule
                ],
            }
        )

        self.assertEqual([r["id"] for r in outcome.preferences.retired], [drop])
        self.assertIn("你在對話中撤回", self.store.get_preference_rule(drop)["retire_reason"])
        self.assertIn("要撤回", self.store.get_preference_rule(drop)["retire_reason"])
        self.assertEqual(self.store.get_preference_rule(keep)["status"], "adopted")
        self.assertEqual(self.store.get_preference_rule(foreign)["status"], "adopted")
        self.assertEqual(outcome.digest["discarded_ungrounded"], 3)

    def test_only_the_first_few_new_rules_per_day_are_adopted_the_rest_wait(self) -> None:
        self._day("一", "二", "三", "四", "五", "六", "七")
        candidates = [self._pref(f"規則 {i}", [i]) for i in range(1, 8)]

        outcome, _ = self._run({"summary": "s", "preference_candidates": candidates})

        self.assertEqual(len(outcome.preferences.adopted), 5)
        self.assertEqual(len(outcome.preferences.waiting), 2)
        self.assertEqual(len(self.store.list_preference_rules(statuses=("pending",))), 2)
        self.assertNotIn("規則 6", preference_context_for(self.store, "proj"))

    def test_a_rule_the_human_brings_back_after_retiring_it_is_adopted_again(self) -> None:
        old = self._rule("proj", "回覆簡短")
        self.store.update_preference_rule(old, status="retired", expected_statuses=("adopted",), retire_reason="x")
        self._day("還是回覆簡短一點")

        outcome, _ = self._run({"summary": "s", "preference_candidates": [self._pref("回覆簡短", [1])]})

        self.assertEqual(len(outcome.preferences.adopted), 1)
        self.assertEqual(self.store.get_preference_rule(old)["status"], "retired")

    def test_context_numbers_the_rules_and_keeps_only_the_newest(self) -> None:
        from memtrace_harness.memory_digest import MAX_PREFERENCES_IN_CONTEXT

        for i in range(MAX_PREFERENCES_IN_CONTEXT + 3):
            self._rule("proj", f"規則 {i}")

        text = preference_context_for(self.store, "proj", with_ids=True)

        self.assertEqual(text.count("- [#"), MAX_PREFERENCES_IN_CONTEXT)
        self.assertIn(f"規則 {MAX_PREFERENCES_IN_CONTEXT + 2}", text)
        self.assertNotIn("規則 0\n", text + "\n")

    def test_the_operator_can_restore_a_retired_rule(self) -> None:
        rule_id = self._rule("proj", "回覆簡短")
        self.store.update_preference_rule(rule_id, status="retired", expected_statuses=("adopted",), retire_reason="x")

        rule = resolve_preference(self.store, rule_id, "restore")

        self.assertEqual((rule["status"], rule["adopted_by"]), ("adopted", "operator"))
        self.assertEqual(rule["retire_reason"], "")  # the old reason no longer applies
        with self.assertRaisesRegex(ValueError, "is adopted, cannot restore"):
            resolve_preference(self.store, rule_id, "restore")


class ChatPreferenceCorrectionTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TraceStore(Path(self._tmp.name) / "t.sqlite3")
        self.rule = self.store.add_preference_candidate(
            project="proj", scope="global", category="communication", text="回覆簡短", evidence=[],
            explicit=True, source_digest_date="2026-09-29", status="adopted", adopted_by="harness-digest",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_retire_and_replace(self) -> None:
        said = apply_chat_preference_correction(self.store, "proj", f"{self.rule}::retire", "那個不用了", "2026-10-02")
        self.assertIn(f"已撤回偏好 #{self.rule}", said)
        retired = self.store.get_preference_rule(self.rule)
        self.assertEqual(retired["status"], "retired")
        self.assertIn("你在聊天中更正：那個不用了", retired["retire_reason"])

        again = self.store.add_preference_candidate(
            project="proj", scope="project", category="workflow", text="先給結論", evidence=[],
            explicit=True, source_digest_date="2026-09-29", status="adopted", adopted_by="harness-digest",
        )
        said = apply_chat_preference_correction(
            self.store, "proj", f"#{again}::replace::先給結論再補充細節", "改成先給結論再補充細節", "2026-10-02"
        )
        self.assertIn("先給結論 → 先給結論再補充細節", said)
        [new] = [r for r in self.store.list_preference_rules(statuses=("adopted",))]
        self.assertEqual((new["text"], new["scope"], new["category"], new["adopted_by"]), ("先給結論再補充細節", "project", "workflow", "harness-chat"))
        self.assertEqual(new["evidence"][0]["quote"], "改成先給結論再補充細節")
        self.assertIn(f"被 #{new['id']} 取代", self.store.get_preference_rule(again)["retire_reason"])

    def test_anything_stale_foreign_or_malformed_is_ignored(self) -> None:
        other = self.store.add_preference_candidate(
            project="other", scope="project", category="other", text="別專案", evidence=[],
            explicit=True, source_digest_date="2026-09-29", status="adopted", adopted_by="harness-digest",
        )
        for payload in ("", "abc::retire", f"{self.rule}", f"{self.rule}::explode", f"{self.rule}::replace::", f"{other}::retire", "9999::retire"):
            self.assertIsNone(apply_chat_preference_correction(self.store, "proj", payload, "x", "2026-10-02"), payload)
        apply_chat_preference_correction(self.store, "proj", f"{self.rule}::retire", "x", "2026-10-02")
        self.assertIsNone(apply_chat_preference_correction(self.store, "proj", f"{self.rule}::retire", "x", "2026-10-02"))
        self.assertEqual(self.store.get_preference_rule(other)["status"], "adopted")


class DigestModelSelectionTests(TestCase):
    def _config(self, **kwargs):
        from memtrace_harness.config import HarnessConfig

        return HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None,
            trace_db_path=Path("t.sqlite3"), trace_root=Path("."),
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900,
            telegram_bot_token=None, telegram_allowed_chat_ids=set(),
            project_index_path=None, chat_provider="claude", chat_model="haiku",
            unattended_write_requires_approval=True, **kwargs,
        )

    def test_unset_digest_model_reuses_chat_candidates(self) -> None:
        config = self._config()
        self.assertEqual(config.digest_candidates_for("p"), config.chat_candidates_for("p"))

    def test_digest_model_is_independent_of_chat(self) -> None:
        config = self._config(
            digest_provider="claude", digest_model="sonnet", digest_fallbacks=(("codex", "gpt-6"),)
        )
        self.assertEqual(
            config.digest_candidates_for("p"), (("claude", "sonnet"), ("codex", "gpt-6"))
        )
        self.assertEqual(config.chat_candidates_for("p"), (("claude", "haiku"),))

    def test_project_override_beats_shared_digest_model(self) -> None:
        import os
        from unittest.mock import patch

        config = self._config(digest_provider="claude", digest_model="sonnet")
        env = {
            "HARNESS_DIGEST_PROVIDER_MYPROJ": "codex",
            "HARNESS_DIGEST_MODEL_MYPROJ": "gpt-6",
            "HARNESS_DIGEST_FALLBACKS_MYPROJ": "claude/opus",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(
                config.digest_candidates_for("myproj"), (("codex", "gpt-6"), ("claude", "opus"))
            )


class FormatLogCompletenessTest(TestCase):
    def test_day_that_fits_the_budget_is_not_truncated(self) -> None:
        from memtrace_harness.memory_digest import _format_log

        long_report = "報告" * 1500  # 3000 chars, over the 1200 per-turn cap
        turns = [{"turn_seq": 1, "speaker": "work_session_report", "content": long_report}]
        self.assertNotIn("截斷", _format_log(turns))
        self.assertIn(long_report, _format_log(turns))

    def test_oversized_day_falls_back_to_per_turn_caps(self) -> None:
        from memtrace_harness.memory_digest import _format_log

        turns = [
            {"turn_seq": i, "speaker": "work_session_report", "content": "x" * 15_000}
            for i in range(20)
        ]
        self.assertIn("截斷", _format_log(turns))
