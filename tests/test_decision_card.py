from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.decision_card import (
    CARD_SCHEMA,
    answer_for_choice,
    card_from_artifact,
    normalize_card,
    parse_pick_callback,
    pick_keyboard_rows,
    render_card,
)
from memtrace_harness.output_contracts import PROFILE_SCHEMAS, output_schema_path
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


def good_card(**overrides):
    card = {
        "situation": "Plan 要求改 schema，但沒說舊資料要不要遷移。",
        "options": [
            {"label": "只改新資料", "action": "新欄位只對新紀錄生效", "tradeoff": "舊資料維持舊格式"},
            {"label": "一併遷移", "action": "寫遷移腳本處理舊資料", "tradeoff": "多一次寫入、需要備份"},
        ],
        "recommended": 0,
        "reason": "舊資料沒有讀取端依賴，遷移風險大於收益。",
        "default_if_silent": "任務維持暫停。",
    }
    card.update(overrides)
    return card


class NormalizeTests(TestCase):
    def test_a_well_formed_card_is_accepted(self) -> None:
        self.assertEqual(normalize_card(good_card())["recommended"], 0)

    def test_unusable_cards_are_dropped_rather_than_raised(self) -> None:
        one_option = good_card(options=good_card()["options"][:1])
        four = good_card(options=good_card()["options"] * 2)
        for bad in (
            None,
            "text",
            {},
            one_option,
            four,
            good_card(recommended=2),
            good_card(recommended=True),
            good_card(recommended=-1),
            good_card(situation="  "),
            good_card(default_if_silent=""),
            good_card(options=[{"label": "x", "action": "y"}, good_card()["options"][0]]),
        ):
            self.assertIsNone(normalize_card(bad), bad)

    def test_a_card_is_only_used_when_the_stage_itself_hands_over_to_a_human(self) -> None:
        card = good_card()
        self.assertIsNotNone(card_from_artifact({"status": "needs_human", "decision_card": card}))
        self.assertIsNotNone(card_from_artifact({"verdict": "NEEDS_HUMAN", "decision_card": card}))
        self.assertIsNotNone(card_from_artifact({"verdict": "REJECT", "decision_card": card}))
        self.assertIsNotNone(card_from_artifact({"action": "ask_human", "decision_card": card}))
        # A Controller that picked "merge" and filled a card in anyway is not asking anyone.
        self.assertIsNone(card_from_artifact({"action": "merge", "decision_card": card}))
        self.assertIsNone(card_from_artifact({"status": "ready", "decision_card": card}))
        self.assertIsNone(card_from_artifact({"status": "needs_human", "decision_card": None}))
        self.assertIsNone(card_from_artifact(None))


class SchemaTests(TestCase):
    def test_every_stage_schema_carries_the_same_required_card(self) -> None:
        for profile in PROFILE_SCHEMAS:
            schema = json.loads(output_schema_path(profile).read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["decision_card"], CARD_SCHEMA, profile)
            self.assertIn("decision_card", schema["required"], profile)


class RenderTests(TestCase):
    def test_card_renders_options_recommendation_and_default(self) -> None:
        text = render_card(normalize_card(good_card()))
        self.assertIn("A. 只改新資料 ⭐建議", text)
        self.assertIn("B. 一併遷移", text)
        self.assertNotIn("B. 一併遷移 ⭐", text)
        self.assertIn("建議 A：", text)
        self.assertIn("不回應的話：任務維持暫停。", text)

    def test_pick_callback_round_trips_and_resumes_with_the_option_as_the_answer(self) -> None:
        card = normalize_card(good_card())
        rows = pick_keyboard_rows("appr_abc123abc123", card)
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0][0]["text"].startswith("⭐ A."))
        data = rows[1][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), 64)
        self.assertEqual(parse_pick_callback(data), ("appr_abc123abc123", 1))
        answer = answer_for_choice(card, 1)
        self.assertIn("方案 B", answer)
        self.assertIn("寫遷移腳本處理舊資料", answer)
        self.assertIsNone(answer_for_choice(card, 2))
        self.assertIsNone(parse_pick_callback("approve:appr_x"))
        self.assertIsNone(parse_pick_callback("pick:appr_x:not-a-number"))


class ApprovalWiringTests(TestCase):
    def _manager(self, tmp: str) -> ApprovalManager:
        return ApprovalManager(TraceStore(Path(tmp) / "t.sqlite3"), {1})

    def _request(self, mgr: ApprovalManager, tmp: str, card, reason="ambiguous_requirement"):
        return mgr.request_approval(
            conversation_id="conv_1",
            workspace="ws",
            working_directory=tmp,
            reason=reason,
            proposed_action="RAW DUMP OF FINDINGS",
            decision_card=card,
        )

    def test_card_is_persisted_and_replaces_the_raw_dump_in_the_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            req = self._request(mgr, tmp, normalize_card(good_card()))
            reloaded = mgr.get_request(req.id)
            self.assertEqual(reloaded.decision_card, normalize_card(good_card()))
            message = reloaded.format_telegram_message()
            self.assertIn("需要你決定", message)
            self.assertIn("A. 只改新資料", message)
            self.assertNotIn("RAW DUMP", message)
            self.assertEqual(mgr.get_pending_for_conversation("conv_1").decision_card, reloaded.decision_card)

    def test_without_a_card_the_old_message_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            req = self._request(mgr, tmp, None)
            message = mgr.get_request(req.id).format_telegram_message()
            self.assertIn("需要你回答問題", message)
            self.assertIn("RAW DUMP", message)

    def test_a_card_never_changes_a_request_that_is_not_a_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = self._manager(tmp)
            req = self._request(mgr, tmp, normalize_card(good_card()), reason="model_output_invalid")
            self.assertIn("系統技術性錯誤", mgr.get_request(req.id).format_telegram_message())

    def test_keyboard_has_one_button_per_option_plus_give_up(self) -> None:
        card = normalize_card(good_card())
        keyboard = TelegramGateway._approval_keyboard("appr_1", "ambiguous_requirement", card)
        self.assertEqual([row[0]["callback_data"] for row in keyboard],
                         ["pick:appr_1:0", "pick:appr_1:1", "reject:appr_1"])
        plain = TelegramGateway._approval_keyboard("appr_1", "ambiguous_requirement", None)
        self.assertEqual([row[0]["callback_data"] for row in plain], ["reject:appr_1"])
