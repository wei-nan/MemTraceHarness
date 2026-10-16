"""The Harness API, with a real service behind it and a real HTTP client in front."""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.api import HarnessApi
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from memtrace_harness.workspace import models_file, parse_env_file, read_model_settings
from memtrace_services.client import HarnessClient, HarnessError, HarnessUnavailable
from tests.workspace_support import workspace

TOKEN = "test-api-token"


class ApiTestCase(TestCase):
    def setUp(self) -> None:
        ctx = workspace({"HARNESS_API_TOKEN": TOKEN, "HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"})
        self.root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.config = replace(config, api_port=0)
        self.scope = ProjectScope.from_file(self.root / "harness-scope.md")
        self.parts = build_harness(self.config, self.scope)
        self.service = self.parts.service
        self.api = HarnessApi(self.service, self.config, self.scope)
        self.server = self.api.start()
        self.addCleanup(self.api.stop)
        self.addCleanup(self.server.server_close)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = HarnessClient(self.url, TOKEN)

    def _raw(self, method, path, body=None, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class AuthTests(ApiTestCase):
    def test_everything_but_health_needs_the_token(self) -> None:
        self.assertEqual(self._raw("GET", "/v1/health", token=None)[0], 200)
        for method, path in (("GET", "/v1/status"), ("GET", "/v1/events"), ("POST", "/v1/messages"), ("POST", "/v1/actions")):
            with self.subTest(path=path):
                self.assertEqual(self._raw(method, path, {} if method == "POST" else None, token=None)[0], 401)
                self.assertEqual(self._raw(method, path, {} if method == "POST" else None, token="wrong")[0], 401)

    def test_it_will_not_start_without_a_token(self) -> None:
        api = HarnessApi(self.service, replace(self.config, api_token=None), self.scope)
        with self.assertRaises(RuntimeError):
            api.start()

    def test_a_malformed_body_is_a_400_not_a_crash(self) -> None:
        self.assertEqual(self._raw("POST", "/v1/messages", {"text": ""})[0], 400)
        self.assertEqual(self._raw("POST", "/v1/actions", {})[0], 400)
        self.assertEqual(self._raw("POST", "/v1/nope", {})[0], 404)


class ConversationTests(ApiTestCase):
    def test_a_message_is_answered_through_the_outbox_not_the_response(self) -> None:
        self.client.post_message("status", audience="tg:1")
        events = self.client.events(wait=5)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["kind"], events[0]["audience"]), ("message", "tg:1"))
        self.assertIn("Harness 已上線", events[0]["text"])

    def test_a_message_may_carry_files_that_are_saved_in_the_workspace_inbox(self) -> None:
        import base64

        data = base64.b64encode(b"hello file").decode()
        status, _ = self._raw("POST", "/v1/messages", {"text": "", "audience": "tg:1", "attachments": [
            {"name": "../note.txt", "mime": "text/plain", "data_b64": data}]})
        self.assertEqual(status, 202)  # a file with no words is still a message
        inbox = self.root / ".harness" / "inbox"
        saved = list(inbox.iterdir())
        self.assertEqual([p.read_bytes() for p in saved], [b"hello file"])
        self.assertTrue(saved[0].name.endswith("_note.txt"))  # the name cannot leave the inbox
        self.assertEqual(self._raw("POST", "/v1/messages", {"text": "x", "attachments": [{"name": "a", "data_b64": "!!"}]})[0], 400)
        self.assertEqual(self._raw("POST", "/v1/messages", {"text": ""})[0], 400)  # no words and no file

    def test_events_for_the_web_page_and_for_telegram_are_kept_apart(self) -> None:
        self.service.send_message("web:main", "to the page")
        self.service.send_message("tg:1", "to telegram")
        self.service.send_message(None, "to everyone")
        self.assertEqual([e["text"] for e in self.client.events(consumer="web")], ["to the page"])
        self.assertEqual([e["text"] for e in self.client.events()], ["to telegram", "to everyone"])

    def test_messages_are_handled_in_the_order_they_arrived(self) -> None:
        for i in range(3):
            self.client.post_message("/schedules", audience=f"tg:{i}")
        deadline = time.monotonic() + 5
        seen: list[dict] = []
        while len(seen) < 3 and time.monotonic() < deadline:
            seen += self.client.events(wait=1)
        self.assertEqual([e["audience"] for e in seen], ["tg:0", "tg:1", "tg:2"])

    def test_the_long_poll_returns_as_soon_as_something_is_said(self) -> None:
        threading.Timer(0.3, lambda: self.service.notify_operator("late news")).start()
        started = time.monotonic()
        events = self.client.events(wait=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(events[0]["text"], "late news")

    def test_an_unacknowledged_event_is_offered_again_only_after_its_lease(self) -> None:
        self.service.notify_operator("once")
        first = self.client.events()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.client.events(), [])  # leased to the first fetch
        self.assertEqual(self.client.ack(first[0]["id"], ok=True), "delivered")
        self.assertEqual(self.client.events(), [])

    def test_a_failed_delivery_is_retried_then_given_up_on(self) -> None:
        self.service.notify_operator("never arrives")
        for _ in range(self.parts.trace_store.OUTBOX_MAX_ATTEMPTS - 1):
            [event] = self.client.events()
            self.assertEqual(self.client.ack(event["id"], ok=False), "retry")
        [event] = self.client.events()
        self.assertEqual(self.client.ack(event["id"], ok=False), "failed")
        self.assertEqual(self.client.events(), [])

    def test_a_delivered_approval_remembers_its_message_and_a_reply_to_it_finds_the_approval(self) -> None:
        req = self.parts.approval_manager.request_approval(
            conversation_id="conv_1", workspace="ws_test", working_directory=str(self.root),
            reason="git_push", proposed_action="git push",
        )
        self.service.notify_approval_request(req)
        [event] = self.client.events()
        self.assertEqual([b["id"] for row in event["actions"] for b in row], [f"approve:{req.id}", f"reject:{req.id}"])
        self.client.ack(event["id"], ok=True, message_refs=["tg:1:42"])
        self.assertEqual(self.parts.approval_manager.get_by_message_ref("tg:1:42").id, req.id)

    def test_a_button_tap_resolves_the_approval_and_clears_the_buttons(self) -> None:
        req = self.parts.approval_manager.request_approval(
            conversation_id="conv_2", workspace="ws_other", working_directory=str(self.root),
            reason="git_push", proposed_action="git push",
        )
        self.parts.approval_manager.record_message_ref(req.id, "tg:1:77")
        toast = self.client.post_action(f"reject:{req.id}", audience="tg:1", message_ref="tg:1:77")
        self.assertIn("rejected", toast)
        self.assertEqual(self.parts.approval_manager.get_request(req.id).status, "rejected")
        kinds = [(e["kind"], e.get("ref")) for e in self.client.events()]
        self.assertIn(("clear", "tg:1:77"), kinds)

    def test_the_commands_are_listed(self) -> None:
        self.assertIn("status", {c["command"] for c in self.client.commands()})


class StatusAndAdminTests(ApiTestCase):
    def test_status_describes_this_harness_and_its_one_project(self) -> None:
        status = self.client.status()
        self.assertEqual([p["name"] for p in status["projects"]], ["ws"])
        self.assertEqual(status["harness"]["workspace_dir"], str(self.root.resolve()))
        self.assertEqual(status["harness"]["outbox_backlog"], 0)
        self.service.notify_operator("waiting")
        self.assertEqual(self.client.status()["harness"]["outbox_backlog"], 1)

    def test_changes_wakes_when_something_is_published(self) -> None:
        version = self.client.changes(-1)
        threading.Timer(0.3, self.api.bus.publish).start()
        self.assertNotEqual(self.client.changes(version, wait=10), version)

    def test_a_model_setting_is_saved_to_the_workspace_and_effective_at_once(self) -> None:
        self.client.admin("chat-model", {"provider": "codex", "model": "gpt-6-luna"})
        self.assertEqual(read_model_settings(models_file(self.root))["HARNESS_CHAT_MODEL"], "gpt-6-luna")
        self.assertNotIn("HARNESS_CHAT_MODEL", parse_env_file(self.root / ".harness.env"))  # the secrets file is untouched
        self.assertEqual(self.config.chat_candidates_for()[0], ("codex", "gpt-6-luna"))
        with self.assertRaises(HarnessError) as caught:
            self.client.admin("chat-model", {"provider": "openai", "model": "x"})
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(HarnessError):
            self.client.admin("chat-model", {"provider": "claude"})  # missing model
        with self.assertRaises(HarnessError) as unknown:
            self.client.admin("no-such-setting", {})
        self.assertEqual(unknown.exception.status, 404)

    def test_an_approval_can_be_resolved_from_the_page_without_a_chat(self) -> None:
        req = self.parts.approval_manager.request_approval(
            conversation_id="conv_3", workspace="ws_other", working_directory=str(self.root),
            reason="git_push", proposed_action="x",
        )
        result = self.client.admin("approval", {"request_id": req.id, "action": "reject"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.parts.approval_manager.get_request(req.id).status, "rejected")
        with self.assertRaises(HarnessError):
            self.client.admin("approval", {"request_id": "appr_nope", "action": "approve"})
        with self.assertRaises(HarnessError):
            self.client.admin("approval", {"request_id": req.id, "action": "explode"})

    def test_a_question_can_be_answered_from_the_page_by_text_or_by_option(self) -> None:
        from unittest.mock import patch

        card = {
            "situation": "s", "recommended": 0, "reason": "r", "default_if_silent": "d", "basis": [],
            "options": [
                {"label": "A", "action": "do a", "tradeoff": "t"},
                {"label": "B", "action": "do b", "tradeoff": "t"},
            ],
        }
        manager = self.parts.approval_manager
        first = manager.request_approval(
            conversation_id="conv_q1", workspace=self.scope.workspace_id, working_directory=str(self.root),
            reason="ambiguous_requirement", proposed_action="q", decision_card=card,
        )
        second = manager.request_approval(
            conversation_id="conv_q2", workspace=self.scope.workspace_id, working_directory=str(self.root),
            reason="ambiguous_requirement", proposed_action="q", decision_card=card,
        )
        with patch.object(self.service, "_resume_approved_conversation") as resume:
            # A bare approve is still refused, and an empty answer is not an answer.
            self.client.admin("approval", {"request_id": first.id, "action": "approve"})
            self.assertEqual(manager.get_request(first.id).status, "pending")
            with self.assertRaises(HarnessError):
                self.client.admin("approval", {"request_id": first.id, "action": "clarify", "answer": "  "})
            self.client.admin("approval", {"request_id": first.id, "action": "clarify", "answer": "只改 Dashboard"})
            self.assertEqual(manager.get_request(first.id).status, "approved")
            self.assertEqual(resume.call_args.args[1], "只改 Dashboard")
            self.client.admin("approval", {"request_id": second.id, "action": "choose", "choice": 1})
            self.assertEqual(manager.get_request(second.id).status, "approved")
            self.assertIn("方案 B", resume.call_args.args[1])
            with self.assertRaises(HarnessError):
                self.client.admin("approval", {"request_id": second.id, "action": "choose", "choice": "x"})

    def test_the_status_lists_everything_waiting_on_the_operator(self) -> None:
        manager = self.parts.approval_manager
        manager.request_approval(
            conversation_id="conv_w", workspace=self.scope.workspace_id, working_directory=str(self.root),
            reason="git_push", proposed_action="push it",
        )
        store = manager.trace_store
        store.create_scope_change_request(
            request_id="scope_w", project=self.scope.name, change={"kind": "probe_host", "value": "a.example.com"},
            summary="probe_host: a.example.com", expires_at="2999-01-01T00:00:00+00:00",
        )
        store.set_scan_snapshot(self.scope.name, {"open": 3, "queued": [{"id": "gh:x/y#1", "title": "t"}], "held": []})
        store.acquire_workspace_lock(self.scope.workspace_id, "conv_holder")
        waiting = self.client.status()["projects"][0]["waiting"]
        self.assertEqual([i["kind"] for i in waiting["items"]], ["approval", "confirm"])
        self.assertEqual(waiting["total"], 2)
        self.assertEqual(waiting["items"][0]["label"], "待核准")
        self.assertEqual(waiting["backlog"]["open"], 3)
        # Who holds the workspace is read live, and a holder with no running thread is reported as not alive.
        self.assertEqual(
            [(h["conversation_id"], h["alive"]) for h in waiting["lock_holders"]], [("conv_holder", False)]
        )

    def test_an_interrupted_task_can_be_abandoned_but_a_running_one_cannot(self) -> None:
        from datetime import datetime, timedelta, timezone

        from memtrace_harness.inflight import default_tracker

        store = self.parts.approval_manager.trace_store
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        for conv in ("conv_dead", "conv_live"):
            store.ensure_conversation(conv, "t", self.scope.workspace_id) if hasattr(store, "ensure_conversation") else None
        with store._connection() as conn:
            for conv in ("conv_dead", "conv_live"):
                conn.execute(
                    "INSERT OR REPLACE INTO conversations (id, task_id, workspace_id, state, created_at, updated_at, last_activity_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?)", (conv, conv, self.scope.workspace_id, old, old, old),
                )
        store.acquire_workspace_lock(self.scope.workspace_id, "conv_dead")
        waiting = self.client.status()["projects"][0]["waiting"]["items"]
        ids = {i["id"]: i for i in waiting if i["kind"] == "interrupted"}
        self.assertEqual(set(ids), {"conv_dead", "conv_live"})
        self.assertTrue(ids["conv_dead"]["holds_lock"])

        class _Thread:
            ident = 1

        default_tracker.register(_Thread(), workspace_id=self.scope.workspace_id, conversation_id="conv_live", trace_store=store)
        self.addCleanup(default_tracker.unregister, _Thread())
        try:
            with self.assertRaises(HarnessError):
                self.client.admin("stuck", {"conversation_id": "conv_live", "action": "abandon"})
            self.client.admin("stuck", {"conversation_id": "conv_dead", "action": "abandon"})
        finally:
            with default_tracker._lock:
                default_tracker._entries.clear()
        self.assertEqual(store.list_workspace_locks(self.scope.workspace_id), [])
        self.assertEqual(
            [c["id"] for c in store.list_unfinished_conversations(self.scope.workspace_id, "2000")], ["conv_live"]
        )

    def test_an_open_chat_question_is_listed_and_can_be_answered_from_the_page(self) -> None:
        from unittest.mock import patch

        store = self.parts.approval_manager.trace_store
        store.create_chat_question(
            question_id="cq_t1", project=self.scope.name, question="先做哪個？",
            options=[{"label": "A", "text": "先做 A"}, {"label": "B", "text": "先做 B"}],
        )
        items = self.client.status()["projects"][0]["waiting"]["items"]
        question = next(i for i in items if i["kind"] == "question")
        self.assertEqual((question["label"], [o["label"] for o in question["options"]]), ("待你選擇", ["A", "B"]))
        with patch.object(self.service, "handle_message") as handle:
            self.client.admin("chat-question", {"question_id": "cq_t1", "choice": 0})
            time.sleep(0.3)
            handle.assert_called_once()
            self.assertEqual(handle.call_args.args[0], "先做 A")
        with self.assertRaises(HarnessError):
            self.client.admin("chat-question", {"question_id": "cq_missing", "choice": 0})
        self.assertFalse([i for i in self.client.status()["projects"][0]["waiting"]["items"] if i["kind"] == "question"])

    def test_an_unknown_turn_is_a_404(self) -> None:
        self.assertIsNone(self.client.turn(99999))


class ClientTests(TestCase):
    def test_a_harness_that_is_not_running_is_unavailable_not_an_error_status(self) -> None:
        with self.assertRaises(HarnessUnavailable):
            HarnessClient("http://127.0.0.1:1", "t", timeout=1).health()


class SharedMemoryApiTests(ApiTestCase):
    def test_only_operator_can_manage_shared_memory_and_stale_edits_are_refused(self):
        path = '/v1/admin/shared-memory'
        body = {'action': 'create', 'title': '共用', 'body': 'worktree'}
        self.assertEqual(self._raw('POST', path, body, token=None)[0], 401)
        self.assertEqual(self._raw('POST', path, body, token='peer-token')[0], 401)
        status, result = self._raw('POST', path, body)
        self.assertEqual(status, 200)
        entry = result['entry']
        from unittest.mock import patch
        with patch('memtrace_harness.admin.merge_model_catalog', return_value={}):
            status, page = self._raw('GET', '/v1/status')
        self.assertEqual(page['shared_memory']['entries'][0]['id'], entry['id'])
        self.assertEqual(self._raw('POST', path, dict(action='toggle', id=entry['id'], revision=1, enabled=False))[0], 200)
        self.assertEqual(self._raw('POST', path, dict(action='toggle', id=entry['id'], revision=1, enabled=True))[0], 400)
        self.assertEqual(self._raw('POST', path, dict(action='remove', id={}, revision=2))[0], 400)


class ExecutionSourceApiTests(ApiTestCase):
    def test_registry_and_per_harness_grants_are_operator_only_and_model_chain_keeps_source(self):
        from unittest.mock import patch
        body = dict(action='save', id='work', provider='codex', label='工作帳號', command='/opt/wrappers/work', quota_bucket='work-account', max_concurrency=2)
        route = '/v1/admin/execution-source'
        self.assertEqual(self._raw('POST', route, body, token=None)[0], 401)
        self.assertEqual(self._raw('POST', route, body, token='peer-token')[0], 401)
        status, result = self._raw('POST', route, body)
        self.assertEqual(status, 200)
        chain = dict(chain=[dict(provider='codex', source='work', model='model-a')])
        self.assertEqual(self._raw('POST', '/v1/admin/chat-chain', chain)[0], 400)
        self.assertEqual(self._raw('POST', '/v1/admin/execution-source-allowlist', dict(allowed=['work']))[0], 200)
        self.assertEqual(self._raw('POST', '/v1/admin/chat-chain', chain)[0], 200)
        with patch('memtrace_harness.admin.merge_model_catalog', return_value={}):
            status, page = self._raw('GET', '/v1/status')
        self.assertEqual(page['projects'][0]['chat_candidates'][0], dict(provider='codex', source='work', model='model-a'))
        self.assertEqual(page['execution_sources']['entries'][0]['id'], 'work')
        self.assertEqual(self._raw('POST', route, dict(action='toggle', id='work', revision=0, enabled=False))[0], 400)
        self.assertEqual(self._raw('POST', route, dict(action='toggle', id='work', revision=1, enabled=False))[0], 200)
        self.assertEqual(self._raw('POST', '/v1/admin/chat-chain', dict(chain=[dict(provider='codex', source=[], model='model-a')]))[0], 400)
