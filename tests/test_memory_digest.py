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
        digest, _ = ground_digest(
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
        _, prefs = ground_digest(
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
        digest, _ = ground_digest(
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

    def test_run_saves_digest_and_queues_candidates_with_the_humans_own_words(self) -> None:
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

        self.assertEqual(outcome.candidates_added, 1)
        self.assertIn("#1 [user] 以後回覆都用繁體中文", calls[0])
        saved = self.store.get_memory_digest("proj", "2026-09-30")
        self.assertEqual(saved["digest"]["summary"], "討論語言")
        self.assertEqual(saved["provider"], "claude")
        [pending] = self.store.list_preference_rules(statuses=("pending",))
        self.assertEqual(pending["evidence"][0]["quote"], "以後回覆都用繁體中文")
        # A candidate never takes effect by itself.
        self.assertEqual(preference_context_for(self.store, "proj"), "")

        # Re-running the same day does not queue the same candidate twice.
        run_digest_for_date(self.store, "proj", SESSION, "2026-09-30", TZ, call)
        self.assertEqual(len(self.store.list_preference_rules(statuses=("pending",))), 1)

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
