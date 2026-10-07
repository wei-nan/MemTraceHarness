from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import cli
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.kb_gardening import (
    CHARTER_MAX_CHARS,
    GARDEN_INTERVAL,
    MAX_DELETES,
    MIN_NODES,
    RETRY_AFTER_FAILURE,
    GardenOutcome,
    apply_ops,
    build_prompt,
    describe_outcome,
    garden_due,
    is_protected,
    run_gardening_pass,
    title_groups,
    undo_gardening_run,
    workspaces_to_garden,
    write_charter,
)
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore


def node(i, title=None, **kw):
    n = {
        "id": f"mem_{i}", "title": title or f"節點 {i}", "body": f"內容 {i}", "content_type": "factual",
        "tags": ["Beri"], "created_at": f"2026-09-{(i % 27) + 1:02d}T00:00:00Z", "resolution_status": "open",
        "pinned": False, "ask_count": 0, "traversal_count": 0,
    }
    n.update(kw)
    return n


class FakeKb:
    def __init__(self, nodes, *, fail_restore=False, fail_delete=()):
        self.nodes = {n["id"]: dict(n) for n in nodes}
        self.deleted: list[tuple] = []
        self.updates: list[dict] = []
        self.created: list[dict] = []
        self.edges: list[tuple] = []
        self.restored: list[str] = []
        self.fail_restore = fail_restore
        self.fail_delete = set(fail_delete)

    def list_nodes(self, *, workspace_id, limit=200, offset=0, stage=None):
        live = [n for n in self.nodes.values() if n["id"] not in {d[0] for d in self.deleted}]
        return live[offset: offset + limit]

    def delete_node(self, *, workspace_id, node_id, reason_category="other", reason_note=None, **kw):
        if node_id in self.fail_delete:
            raise RuntimeError("delete refused")
        self.deleted.append((node_id, reason_category, reason_note))

    def restore_node(self, *, workspace_id, node_id, **kw):
        if self.fail_restore:
            raise RuntimeError("not in trash any more")
        self.restored.append(node_id)

    def update_node(self, **kw):
        self.updates.append(kw)

    def create_node(self, **kw):
        self.created.append(kw)
        return f"mem_new{len(self.created)}"

    def create_edge(self, **kw):
        self.edges.append((kw["from_id"], kw["to_id"], kw["relation"]))
        return True


def store(tmp):
    return TraceStore(Path(tmp) / "t.sqlite3")


def apply(kb, ts, nodes, ops, *, charter_node_id=None):
    run_id = ts.start_gardening_run("ws", "proj")
    return apply_ops(
        kb, ts, run_id=run_id, workspace_id="ws", ops=ops, nodes=nodes, groups=title_groups(nodes),
        charter_node_id=charter_node_id,
    ), run_id


class GroupAndPromptTests(TestCase):
    def test_identical_titles_are_one_group_and_the_prompt_does_not_list_every_id(self) -> None:
        nodes = [node(i, "Harness loop draft: 每10分鐘檢查") for i in range(65)] + [node(100, "真正的決定")]
        groups = title_groups(nodes)
        self.assertEqual(list(groups), ["G1"])
        self.assertEqual(len(groups["G1"]), 65)
        prompt = build_prompt(
            project="p", workspace_id="ws", role_label="規格", purpose="一個專案", charter=None,
            nodes=nodes, groups=groups,
        )
        self.assertIn("G1 | x65 identical titles", prompt)
        self.assertIn("mem_100", prompt)
        self.assertNotIn("mem_30", prompt)
        self.assertIn("(none yet", prompt)

    def test_protection_covers_what_the_harness_manages_and_what_a_person_confirmed(self) -> None:
        self.assertFalse(is_protected(node(1)))
        for kw in (
            {"pinned": True}, {"ask_count": 2}, {"validity_confirmed_by": "me@x"},
            {"tags": ["daily-digest"]}, {"tags": ["task", "status:open"]}, {"tags": ["primary-session"]},
        ):
            self.assertTrue(is_protected(node(1, **kw)), kw)
        self.assertTrue(is_protected(node(1), charter_node_id="mem_1"))


class ApplyOpsTests(TestCase):
    def test_dedupe_keeps_one_removes_the_rest_softly_and_keeps_a_local_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            nodes = [node(i, "重複的草稿") for i in range(1, 5)] + [node(9, "別的")]
            kb = FakeKb(nodes)
            (applied, deleted, skipped), run_id = apply(
                kb, ts, nodes, [{"op": "dedupe", "group": "G1", "keep_id": "mem_4", "reason": "重複"}]
            )
            self.assertEqual((applied, deleted, skipped), ({"dedupe": 3}, 3, []))
            self.assertEqual({d[0] for d in kb.deleted}, {"mem_1", "mem_2", "mem_3"})
            self.assertTrue(all(d[1] == "duplicate" for d in kb.deleted))
            actions = ts.list_gardening_actions(run_id, op="delete")
            self.assertEqual(len(actions), 3)
            for action in actions:   # each action carries that node's own content
                self.assertEqual(action["snapshot"]["body"], f"內容 {action['node_id'].removeprefix('mem_')}")

    def test_protected_nodes_are_never_removed_even_inside_a_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(1, "同標題", pinned=True), node(2, "同標題", ask_count=3), node(3, "同標題", tags=["task"]),
                     node(4, "同標題"), node(5, "同標題")]
            kb = FakeKb(nodes)
            (applied, deleted, skipped), _ = apply(
                kb, store(tmp), nodes, [{"op": "dedupe", "group": "G1", "keep_id": "mem_5"}]
            )
            self.assertEqual({d[0] for d in kb.deleted}, {"mem_4"})
            self.assertEqual(len(skipped), 3)
            self.assertTrue(all("受保護" in s for s in skipped))

    def test_at_most_a_hundred_nodes_are_removed_per_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(i, "洗版") for i in range(1, 151)]
            kb = FakeKb(nodes)
            (applied, deleted, skipped), _ = apply(
                kb, store(tmp), nodes, [{"op": "dedupe", "group": "G1", "keep_id": "mem_1"}]
            )
            self.assertEqual(deleted, MAX_DELETES)
            self.assertTrue(any("上限" in s for s in skipped))

    def test_ids_the_model_was_not_shown_and_unknown_ops_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(1), node(2), node(3)]
            kb = FakeKb(nodes)
            (applied, deleted, skipped), _ = apply(kb, store(tmp), nodes, [
                {"op": "delete", "node_ids": ["mem_999"], "reason_category": "other"},
                {"op": "retag", "node_id": "mem_999", "add": ["x"]},
                {"op": "format_disk"},
                "garbage",
                {"op": "dedupe", "group": "G9", "keep_id": "mem_1"},
            ])
            self.assertEqual((applied, deleted), ({}, 0))
            self.assertEqual(kb.deleted + kb.updates, [])
            self.assertEqual(len(skipped), 5)

    def test_non_destructive_ops_are_applied_as_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(1, tags=["Beri", "舊"]), node(2), node(3), node(4)]
            kb = FakeKb(nodes)
            (applied, deleted, skipped), _ = apply(kb, store(tmp), nodes, [
                {"op": "retag", "node_id": "mem_1", "add": ["已確認"], "remove": ["舊"]},
                {"op": "set_resolution", "node_id": "mem_2", "status": "resolved"},
                {"op": "supersede", "old_id": "mem_3", "new_id": "mem_4"},
                {"op": "link", "from_id": "mem_1", "to_id": "mem_2", "relation": "related_to"},
                {"op": "retitle", "node_id": "mem_4", "title": "新標題"},
                {"op": "pin", "node_id": "mem_1"},
            ])
            self.assertEqual(applied, {"retag": 1, "set_resolution": 1, "supersede": 1, "link": 1, "retitle": 1, "pin": 1})
            self.assertEqual((deleted, skipped), (0, []))
            self.assertEqual(kb.updates[0]["tags"], ["Beri", "已確認"])
            self.assertEqual(kb.updates[1]["resolution_status"], "resolved")
            self.assertIn(("mem_3", "mem_4", "superseded_by"), kb.edges)
            self.assertIn(("mem_1", "mem_2", "related_to"), kb.edges)
            self.assertEqual(kb.updates[-1]["pinned"], True)

    def test_bad_values_are_refused_and_a_failing_op_does_not_stop_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(1), node(2), node(3)]
            kb = FakeKb(nodes, fail_delete={"mem_1"})
            (applied, deleted, skipped), _ = apply(kb, store(tmp), nodes, [
                {"op": "set_resolution", "node_id": "mem_2", "status": "deleted"},
                {"op": "link", "from_id": "mem_2", "to_id": "mem_3", "relation": "owns"},
                {"op": "delete", "node_ids": ["mem_1"], "reason_category": "hallucination"},
                {"op": "retitle", "node_id": "mem_2", "title": "好"},
            ])
            self.assertEqual(applied, {"retitle": 1})
            self.assertEqual(len(skipped), 3)
            self.assertTrue(any("執行失敗" in s for s in skipped))

    def test_a_node_deleted_in_this_pass_cannot_also_be_edited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            nodes = [node(1), node(2)]
            kb = FakeKb(nodes)
            (applied, _, skipped), _ = apply(kb, store(tmp), nodes, [
                {"op": "delete", "node_ids": ["mem_1"]},
                {"op": "retitle", "node_id": "mem_1", "title": "x"},
                {"op": "link", "from_id": "mem_2", "to_id": "mem_1", "relation": "related_to"},
            ])
            self.assertEqual(applied, {"delete": 1})
            self.assertEqual(len(skipped), 2)


class CharterTests(TestCase):
    def test_the_map_is_created_pinned_then_updated_in_place_and_unchanged_text_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb([])
            args = dict(workspace_id="ws", project="Beri", role_label="規格")
            self.assertEqual(write_charter(kb, ts, text="# 地圖\n決策在這", **args), "created")
            self.assertEqual(kb.created[0]["tags"], ["harness", "controller", "charter"])
            self.assertNotIn("draft", kb.created[0]["tags"])
            self.assertEqual(kb.updates[-1]["pinned"], True)
            self.assertEqual(ts.get_workspace_charter("ws")["body"], "# 地圖\n決策在這")

            self.assertIsNone(write_charter(kb, ts, text="# 地圖\n決策在這", **args))
            self.assertEqual(write_charter(kb, ts, text="# 地圖 v2", **args), "updated")
            self.assertEqual(len(kb.created), 1)
            self.assertEqual(kb.updates[-1]["body"], "# 地圖 v2")

    def test_empty_or_oversized_maps_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = FakeKb([])
            for text in ("", "   ", "x" * (CHARTER_MAX_CHARS + 1)):
                self.assertIsNone(write_charter(kb, store(tmp), workspace_id="ws", project="p", role_label="r", text=text))
            self.assertEqual(kb.created, [])

    def test_if_the_map_node_vanished_a_new_one_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb([])
            ts.set_workspace_charter("ws", "mem_gone", "old")
            real_update = kb.update_node
            calls = []

            def flaky(**kw):
                calls.append(kw)
                if kw.get("node_id") == "mem_gone":
                    raise RuntimeError("404")
                real_update(**kw)

            kb.update_node = flaky
            self.assertEqual(write_charter(kb, ts, workspace_id="ws", project="p", role_label="r", text="new"), "created")
            self.assertEqual(ts.get_workspace_charter("ws")["node_id"], "mem_new1")


class PassTests(TestCase):
    def _run(self, kb, ts, reply):
        return run_gardening_pass(
            client=kb, trace_store=ts, workspace_id="ws", project="Beri", role_label="規格",
            purpose="產品", caller=lambda prompt: reply,
        )

    def test_a_pass_tidies_maps_and_records_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            nodes = [node(i, "洗版草稿") for i in range(1, 6)] + [node(10), node(11)]
            kb = FakeKb(nodes)
            reply = json.dumps({
                "charter": "# 地圖\n決策與版本",
                "ops": [{"op": "dedupe", "group": "G1", "keep_id": "mem_5"}, {"op": "pin", "node_id": "mem_10"}],
                "notes": "草稿不該進規格工作區",
            })
            outcome = self._run(kb, ts, reply)
            self.assertEqual((outcome.charter, outcome.deleted, outcome.applied), ("created", 4, {"dedupe": 4, "pin": 1}))
            run = ts.get_gardening_run(outcome.run_id)
            self.assertEqual(run["status"], "ok")
            self.assertEqual(run["summary"]["notes"], "草稿不該進規格工作區")
            self.assertIsNotNone(ts.last_gardening_success("ws"))

    def test_an_unusable_model_answer_is_a_failed_run_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb([node(i) for i in range(5)])
            self.assertIsNone(self._run(kb, ts, "not json"))
            self.assertEqual((kb.deleted, kb.updates, kb.created), ([], [], []))
            self.assertIsNone(ts.last_gardening_success("ws"))
            self.assertIsNotNone(ts.last_gardening_attempt("ws"))

    def test_a_tiny_workspace_is_left_alone_without_calling_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            caller = MagicMock()
            kb = FakeKb([node(i) for i in range(MIN_NODES - 1)])
            self.assertIsNone(run_gardening_pass(
                client=kb, trace_store=store(tmp), workspace_id="ws", project="p", role_label="r",
                purpose="", caller=caller,
            ))
            caller.assert_not_called()

    def test_the_second_pass_is_shown_the_current_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            ts.set_workspace_charter("ws", "mem_map", "# 現有地圖內容")
            prompts = []
            run_gardening_pass(
                client=FakeKb([node(i) for i in range(5)]), trace_store=ts, workspace_id="ws", project="p",
                role_label="r", purpose="", caller=lambda p: prompts.append(p) or json.dumps({"charter": None, "ops": []}),
            )
            self.assertIn("# 現有地圖內容", prompts[0])

    def test_describe_outcome_mentions_the_undo_only_when_something_was_deleted(self) -> None:
        text = describe_outcome(GardenOutcome(1, "ws", "Beri", "規格", charter="created",
                                              applied={"dedupe": 4}, deleted=4, skipped=["x"], notes="學到了"))
        self.assertIn("知識庫地圖已建立", text)
        self.assertIn("合併重複 4", text)
        self.assertIn("整批還原", text)
        self.assertIn("被擋下", text)
        self.assertNotIn("還原", describe_outcome(GardenOutcome(2, "ws", "Beri", "規格")))


class DueTests(TestCase):
    def test_due_never_run_not_due_after_success_due_after_a_week_and_failures_back_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts = store(tmp)
            now = datetime.now(timezone.utc)
            self.assertTrue(garden_due(ts, "ws", now))
            run = ts.start_gardening_run("ws", "p")
            ts.finish_gardening_run(run, "failed")
            self.assertFalse(garden_due(ts, "ws", now))                       # just failed: back off
            self.assertTrue(garden_due(ts, "ws", now + RETRY_AFTER_FAILURE + timedelta(minutes=1)))
            ok = ts.start_gardening_run("ws2", "p")
            ts.finish_gardening_run(ok, "ok")
            self.assertFalse(garden_due(ts, "ws2", now + timedelta(days=3)))
            self.assertTrue(garden_due(ts, "ws2", now + GARDEN_INTERVAL + timedelta(minutes=1)))


class UndoTests(TestCase):
    def _deleted_run(self, ts):
        run_id = ts.start_gardening_run("ws", "p")
        ts.add_gardening_action(run_id, op="delete", node_id="mem_1",
                                snapshot={"title": "舊的", "body": "內容", "content_type": "factual", "tags": ["x"]})
        ts.add_gardening_action(run_id, op="delete", node_id="mem_2", snapshot=None)
        return run_id

    def test_undo_restores_from_the_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb([])
            run_id = self._deleted_run(ts)
            self.assertEqual(undo_gardening_run(kb, ts, run_id), (2, 0, 0))
            self.assertEqual(kb.restored, ["mem_1", "mem_2"])
            self.assertTrue(all(a["undone"] for a in ts.list_gardening_actions(run_id)))
            self.assertEqual(undo_gardening_run(kb, ts, run_id), (0, 0, 0))      # once only

    def test_undo_recreates_from_the_local_copy_when_the_trash_has_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ts, kb = store(tmp), FakeKb([], fail_restore=True)
            run_id = self._deleted_run(ts)
            restored, recreated, failed = undo_gardening_run(kb, ts, run_id)
            self.assertEqual((restored, recreated, failed), (0, 1, 1))           # mem_2 has no local copy
            self.assertEqual(kb.created[0]["title"], "舊的")
            undone = {a["node_id"]: a["undone"] for a in ts.list_gardening_actions(run_id)}
            self.assertEqual(undone, {"mem_1": True, "mem_2": False})


class WorkspaceSelectionTests(TestCase):
    def test_each_workspace_once_and_only_the_projects_own(self) -> None:
        def scope(name, ws):
            return SimpleNamespace(name=name, workspace_id=ws, raw_markdown=f"{name} 說明")

        config = SimpleNamespace(
            memory_workspace_id_for=lambda name, ws: {"Beri": "ws_mem_beri"}.get(name, ws)
        )
        picked = workspaces_to_garden(config, [scope("Beri", "ws_beri"), scope("beri-android", "ws_beri"), scope("TW", "ws_tw")])
        self.assertEqual([(w, p, r) for w, p, r, _ in picked],
                         [("ws_beri", "Beri", "規格"), ("ws_mem_beri", "Beri", "記憶"), ("ws_tw", "TW", "規格")])


def _gateway(tmp, kb=None):
    path = Path(tmp)
    ts = TraceStore(path / "t.sqlite3")
    config = HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=path / "t.sqlite3", trace_root=path,
        claude_command="claude", codex_command="codex", antigravity_command="agy",
        antigravity_output_mode="auto", cli_timeout_seconds=900, telegram_bot_token="fake",
        telegram_allowed_chat_ids={12345}, project_index_path=None, chat_provider="claude",
        chat_model="haiku", unattended_write_requires_approval=True,
    )
    scope = ProjectScope(name="test_proj", workspace_id="ws_test", working_directory=path,
                         scope_file_path=path / "harness-scope.md")
    gw = TelegramGateway(config, ApprovalManager(ts, {12345}), ChatTriage([scope]), PrimarySessionManager(ts),
                         [scope], memtrace_client=kb)
    gw.send_message = MagicMock()
    gw.send_message_with_keyboard = MagicMock(return_value=3)
    gw.clear_message_keyboard = MagicMock()
    gw.answer_callback_query = MagicMock()
    return gw, ts, scope


class GatewayTests(TestCase):
    def test_the_maps_reach_the_context_the_controller_and_the_team_are_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            ts.set_workspace_charter("ws_test", "mem_map", "# 規格地圖：決策放這裡")
            items = gw._project_context_items(scope)
            [item] = [i for i in items if i.ref == "harness:kb-map:ws_test"]
            self.assertIn("決策放這裡", item.body)
            self.assertEqual(item.content_type, "context")

    def test_a_pass_that_deleted_something_is_reported_with_an_undo_button_that_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = FakeKb([])
            gw, ts, scope = _gateway(tmp, kb)
            run_id = ts.start_gardening_run("ws_test", "test_proj")
            ts.add_gardening_action(run_id, op="delete", node_id="mem_1", snapshot=None)
            gw.notify_garden_outcome(GardenOutcome(run_id, "ws_test", "test_proj", "規格", applied={"delete": 1}, deleted=1))
            text, keyboard = gw.send_message_with_keyboard.call_args.args[1:3]
            self.assertIn("整批還原", text)
            self.assertEqual(keyboard[0][0]["callback_data"], f"kb_undo:{run_id}")

            gw._handle_callback_query({"id": "cb", "data": f"kb_undo:{run_id}",
                                       "message": {"chat": {"id": 12345}, "message_id": 4}})
            self.assertEqual(kb.restored, ["mem_1"])
            self.assertIn("已還原 1 個節點", gw.send_message.call_args.args[1])

    def test_a_pass_that_deleted_nothing_has_no_undo_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.notify_garden_outcome(GardenOutcome(1, "ws_test", "test_proj", "規格", charter="created"))
            gw.send_message_with_keyboard.assert_not_called()
            self.assertIn("知識庫地圖已建立", gw.send_message.call_args.args[1])

    def test_the_garden_command_starts_a_pass_for_that_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            gw.garden_now = MagicMock()
            gw.process_update({"update_id": 1, "message": {"chat": {"id": 12345}, "text": "/garden"}})
            gw.garden_now.assert_called_once_with(scope, 12345)
            gw.garden_now = None
            gw.process_update({"update_id": 2, "message": {"chat": {"id": 12345}, "text": "/garden"}})
            self.assertIn("沒有啟用", gw.send_message.call_args.args[1])


class CliPassTests(TestCase):
    def test_due_workspaces_are_tidied_forced_ones_regardless_and_the_operator_is_told(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = FakeKb([node(i, "洗版") for i in range(1, 6)] + [node(i) for i in range(20, 24)])
            gw, ts, scope = _gateway(tmp, kb)
            gw.notify_garden_outcome = MagicMock()
            scope.raw_markdown = "測試專案"
            reply = json.dumps({"charter": "# 地圖", "ops": [{"op": "dedupe", "group": "G1", "keep_id": "mem_5"}]})
            with patch.object(cli, "make_review_caller", return_value=lambda prompt: reply):
                ran = cli.run_knowledge_gardening(gw.config, ts, kb, [scope], {"test_proj": gw})
                self.assertEqual(ran, 1)                       # ws_test: spec (memory ws is the same one)
                gw.notify_garden_outcome.assert_called_once()
                self.assertEqual(len(kb.deleted), 4)
                # Not due again for a week...
                self.assertEqual(cli.run_knowledge_gardening(gw.config, ts, kb, [scope], {"test_proj": gw}), 0)
                # ...unless forced (the /garden command).
                self.assertEqual(
                    cli.run_knowledge_gardening(gw.config, ts, kb, [scope], {"test_proj": gw}, force=True), 1
                )

    def test_without_a_knowledge_base_nothing_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gw, ts, scope = _gateway(tmp)
            self.assertEqual(cli.run_knowledge_gardening(gw.config, ts, None, [scope], {}), 0)
