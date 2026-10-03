from __future__ import annotations

from unittest import TestCase

from memtrace_harness.loop import (
    summarize_gate_artifact,
    summarize_invalid_artifact,
    summarize_plan_needs_human,
)
from memtrace_harness.telegram_gateway import _split_telegram_text


class TelegramSplitTests(TestCase):
    def _joined(self, chunks):
        return "".join(chunks).replace("\n", "").replace(" ", "")

    def test_short_text_is_one_message(self) -> None:
        self.assertEqual(_split_telegram_text("hello"), ["hello"])

    def test_long_text_loses_nothing_and_every_chunk_fits(self) -> None:
        text = "\n\n".join(f"段落{i}：" + "內容" * 300 for i in range(20))
        chunks = _split_telegram_text(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual(self._joined(chunks), text.replace("\n", "").replace(" ", ""))

    def test_prefers_sentence_end_over_cutting_a_sentence(self) -> None:
        text = "這是第一句。" * 400  # no newlines at all
        chunks = _split_telegram_text(text, limit=1000)
        self.assertTrue(all(c.endswith("。") for c in chunks))
        self.assertEqual("".join(chunks), text)

    def test_unbroken_run_is_still_delivered_whole(self) -> None:
        text = "x" * 5000
        chunks = _split_telegram_text(text, limit=1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), text)


class ReportTextTests(TestCase):
    def test_gate_summary_lists_every_finding_and_open_question_in_full(self) -> None:
        long_description = "很長的描述" * 100
        artifact = {
            "verdict": "NEEDS_HUMAN",
            "findings": [{"severity": "high", "description": f"{i}:{long_description}"} for i in range(6)],
            "unverified_items": [f"問題{i}:{long_description}" for i in range(5)],
        }
        text = summarize_gate_artifact("G1", artifact)
        for i in range(6):
            self.assertIn(f"{i}:{long_description}", text)
        for i in range(5):
            self.assertIn(f"問題{i}:{long_description}", text)
        self.assertNotIn("more finding", text)
        self.assertNotIn("more open question", text)

    def test_plan_summary_keeps_the_whole_plan_and_all_questions(self) -> None:
        plan = "步驟" * 3000
        text = summarize_plan_needs_human(
            "Planner", {"plan": plan, "open_questions": [f"q{i}" for i in range(15)]}
        )
        self.assertIn(plan, text)
        self.assertIn("q14", text)

    def test_invalid_artifact_shows_the_whole_raw_output(self) -> None:
        artifact = {"status": "x", "plan": "內容" * 1000}
        text = summarize_invalid_artifact("Planner", artifact)
        self.assertIn("內容" * 1000, text)
