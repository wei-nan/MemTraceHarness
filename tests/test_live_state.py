from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness import memory_digest, stage_skills
from memtrace_harness.live_state import BLOCK_CHAR_LIMIT, collect_live_state
from memtrace_harness.scope import ProjectScope
from memtrace_harness.taiwantrade_mcp import ProxyError

TURNS = [{"turn_seq": 1, "speaker": "user", "content": "hello"}]


def _prompt(**kwargs) -> str:
    return memory_digest.build_digest_prompt("p", "2026-10-13", TURNS, [], **kwargs)


class LiveStateTests(TestCase):
    def test_nothing_configured_means_nothing_called(self) -> None:
        def boom(*a, **k):
            raise AssertionError("must not be called")

        self.assertEqual(collect_live_state(None, call=boom), ("", set()))
        self.assertEqual(collect_live_state([], call=boom), ("", set()))

    def test_replies_become_cited_blocks(self) -> None:
        seen = []

        def fake(name, args, *, base_url, api_key):
            seen.append((name, args))
            return f'{{"tool":"{name}"}}'

        text, ids = collect_live_state(["list_orders", "get_positions", "list_orders"], env={}, call=fake)
        self.assertEqual(seen, [("list_orders", {}), ("get_positions", {})])
        self.assertEqual(ids, {"live:list_orders", "live:get_positions"})
        self.assertIn('[live:list_orders]\n{"tool":"list_orders"}', text)

    def test_failed_or_unusable_queries_are_unknown_and_not_citable(self) -> None:
        def fail(name, args, *, base_url, api_key):
            raise ProxyError("TaiwanTrade API key is not configured for the proxy")

        text, ids = collect_live_state(["list_orders", "get_quotes", "no_such_tool"], env={}, call=fail)
        self.assertEqual(ids, set())
        self.assertEqual(text.count("查詢失敗"), 3)

    def test_long_reply_is_cut(self) -> None:
        text, _ = collect_live_state(["list_orders"], env={}, call=lambda *a, **k: "x" * (BLOCK_CHAR_LIMIT + 50))
        self.assertIn("截斷", text)
        self.assertLess(len(text), BLOCK_CHAR_LIMIT + 200)

    def test_scope_file_declares_the_queries(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-scope.md"
            path.write_text("- workspace_id: ws_x\n- digest_state_queries: list_orders, get_positions\n", encoding="utf-8")
            self.assertEqual(ProjectScope.from_file(path).digest_state_queries, ["list_orders", "get_positions"])
            path.write_text("- workspace_id: ws_x\n", encoding="utf-8")
            self.assertEqual(ProjectScope.from_file(path).digest_state_queries, [])


class DigestRulesSkillTests(TestCase):
    def test_rules_come_from_the_domain_skill(self) -> None:
        self.assertEqual(stage_skills.load_skills()["digest-rules"].kind, "domain")
        self.assertIn(stage_skills.domain_section("digest-rules", "rules"), _prompt())

    def test_prompt_without_live_state_has_no_live_state_wording(self) -> None:
        self.assertNotIn("live:", _prompt())
        self.assertNotIn(stage_skills.domain_section("digest-rules", "live_state_rule"), _prompt())

    def test_live_state_and_its_rule_enter_the_prompt_only_when_given(self) -> None:
        prompt = _prompt(live_state="[live:list_orders]\n[]")
        self.assertIn("[live:list_orders]\n[]", prompt)
        self.assertIn(stage_skills.domain_section("digest-rules", "live_state_rule"), prompt)
        self.assertLess(prompt.index("[live:list_orders]"), prompt.index("Rules:"))

    def test_a_live_id_grounds_a_stale_review_and_an_unknown_one_does_not(self) -> None:
        raw = {"summary": "s", "open_item_review": [{"index": 0, "status": "stale", "reason": "r", "turns": [], "state_refs": ["live:list_orders"]}]}
        carried = [{"text": "confirm the order", "since": "2026-10-12"}]
        def ground(ids):
            return memory_digest.ground_digest(
                raw, digest_date="2026-10-13", turns=TURNS, carried_open_items=carried, adopted_ids=set(), state_ids=ids
            )[0]

        resolved = ground({"live:list_orders"})
        self.assertEqual(resolved["open_items"], [])
        self.assertEqual(resolved["resolved_items"][0]["resolution"], "stale")
        kept = ground(set())
        self.assertEqual(kept["resolved_items"], [])
        self.assertTrue(kept["open_items"][0]["needs_operator"])
