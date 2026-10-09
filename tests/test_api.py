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

    def test_an_unknown_turn_is_a_404(self) -> None:
        self.assertIsNone(self.client.turn(99999))


class ClientTests(TestCase):
    def test_a_harness_that_is_not_running_is_unavailable_not_an_error_status(self) -> None:
        with self.assertRaises(HarnessUnavailable):
            HarnessClient("http://127.0.0.1:1", "t", timeout=1).health()
