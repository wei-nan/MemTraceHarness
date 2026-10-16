"""The status web service in front of real Harness APIs."""

import json
import time
import urllib.error
import urllib.request
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.api import HarnessApi
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from memtrace_harness.workspace import models_file, read_model_settings
from memtrace_services.web.aggregate import Aggregator, HarnessTarget
from memtrace_services.web.main import load_config
from memtrace_services.web.chat import ChatHub
from memtrace_services.web.server import start_server
from tests.workspace_support import workspace


class WebServiceTests(TestCase):
    def _harness(self, name: str, token: str, shared_path=None, sources_path=None):
        ctx = workspace({"HARNESS_API_TOKEN": token, "HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"})
        root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        config = replace(config, api_port=0, **({"shared_memory_path": shared_path} if shared_path else {}))
        if sources_path:
            config = replace(config, execution_sources_path=sources_path)
        scope = replace(ProjectScope.from_file(root / "harness-scope.md"), name=name)
        parts = build_harness(config, scope)
        api = HarnessApi(parts.service, config, scope)
        server = api.start()
        self.addCleanup(api.stop)
        self.addCleanup(server.server_close)
        return root, parts, HarnessTarget(name, f"http://127.0.0.1:{server.server_address[1]}", token)

    def _web(self, targets):
        aggregator = Aggregator(targets)
        server = start_server("127.0.0.1", 0, aggregator, ChatHub([]))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return aggregator, f"http://127.0.0.1:{server.server_address[1]}"

    def _post(self, base, path, body):
        request = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    @patch("memtrace_harness.admin.merge_model_catalog", return_value={})
    def test_shared_memory_has_one_global_editor_and_is_live_in_two_harnesses(self, _catalog):
        with TemporaryDirectory() as tmp:
            shared = Path(tmp) / "shared.sqlite3"
            _, a_parts, a = self._harness("alpha", "ta", shared)
            _, b_parts, b = self._harness("beta", "tb", shared)
            aggregator, base = self._web([a, b])
            self.assertEqual(len(aggregator.collect()["shared_memory"]), 1)
            status, result = self._post(base, "/api/shared-memory", {
                "project": "alpha", "action": "create", "title": "共享規則", "body": "使用 worktree"})
            self.assertEqual(status, 200)
            self.assertIn("使用 worktree", a_parts.service._shared_memory_context())
            self.assertIn("使用 worktree", b_parts.service._shared_memory_context())
            self.assertEqual(aggregator.collect()["shared_memory"][0]["entries"][0]["id"], result["entry"]["id"])

    @patch("memtrace_harness.admin.merge_model_catalog", return_value={})
    def test_global_sources_are_shared_but_each_harness_has_separate_grants(self, _catalog):
        with TemporaryDirectory() as tmp:
            sources = Path(tmp) / "sources.sqlite3"
            _, alpha, a = self._harness("alpha", "ta", sources_path=sources)
            _, beta, b = self._harness("beta", "tb", sources_path=sources)
            aggregator, base = self._web([a, b])
            self.assertEqual(len(aggregator.collect()["execution_sources"]), 1)
            status, result = self._post(base, "/api/execution-source", dict(
                project="alpha", action="save", id="work", provider="codex", label="工作", command="codex-work"))
            self.assertEqual(status, 200)
            self.assertEqual(len(aggregator.collect()["execution_sources"][0]["entries"]), 1)
            status, _ = self._post(base, "/api/execution-source-allowlist", dict(project="alpha", allowed=["work"]))
            self.assertEqual(status, 200)
            self.assertEqual(alpha.service.config.allowed_execution_sources(), ["work"])
            self.assertEqual(beta.service.config.allowed_execution_sources(), [])
            self.assertEqual(self._post(base, "/api/chat-chain", dict(project="beta", chain=[dict(provider="codex", source="work", model="model-a")]))[0], 400)
            self.assertEqual(self._post(base, "/api/chat-chain", dict(project="alpha", chain=[dict(provider="codex", source="work", model="model-a")]))[0], 200)
            self.assertNotIn("@", beta.service.config.chat_candidates_for()[0][0])

    def test_several_harnesses_appear_on_one_page_and_a_down_one_is_flagged(self) -> None:
        _, _, a = self._harness("alpha", "ta")
        _, _, b = self._harness("beta", "tb")
        gone = HarnessTarget("gone", "http://127.0.0.1:1", "tg")
        aggregator, base = self._web([a, b, gone])
        data = aggregator.collect()
        self.assertEqual(sorted(p["name"] for p in data["projects"]), ["alpha", "beta"])
        self.assertEqual({p["name"]: p["harness"] for p in data["projects"]}, {"alpha": "alpha", "beta": "beta"})
        self.assertEqual([(h["name"], h["up"]) for h in data["harnesses"]], [("alpha", True), ("beta", True), ("gone", False)])
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            page = resp.read().decode("utf-8")
        self.assertIn('"alpha"', page)
        self.assertNotIn("__INITIAL_JSON__", page)
        for asset, marker in (("app.js", "function render"), ("app.css", ".tab")):
            with urllib.request.urlopen(base + "/static/" + asset, timeout=5) as resp:
                self.assertIn(marker, resp.read().decode("utf-8"))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(base + "/static/../page.py", timeout=5)

    def test_a_message_typed_on_the_page_is_answered_on_the_page_and_never_reaches_telegram(self) -> None:
        _, parts, target = self._harness("alpha", "ta")
        hub = ChatHub([target])
        hub.start()
        self.addCleanup(hub.stop)
        aggregator = Aggregator([target])
        server = start_server("127.0.0.1", 0, aggregator, hub)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertEqual(self._post(base, "/api/chat-send", {"project": "alpha", "text": "status"})[0], 202)
        self.assertEqual(self._post(base, "/api/chat-send", {"project": "alpha", "text": "  "})[0], 400)
        seen: dict = {"messages": [], "version": 0}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any(m["role"] == "harness" for m in seen["messages"]):
            with urllib.request.urlopen(f"{base}/api/chat?project=alpha&since=0&wait=2", timeout=10) as resp:
                seen = json.loads(resp.read())
        self.assertEqual([m["role"] for m in seen["messages"]][:1], ["user"])
        reply = next(m for m in seen["messages"] if m["role"] == "harness")
        self.assertIn("Harness 已上線", reply["text"])
        self.assertTrue(reply["ref"].startswith("web:"))
        # What the page already showed was delivered; Telegram's side of the outbox has nothing of it.
        self.assertEqual(target.client().events(), [])

    def test_a_setting_is_sent_to_the_harness_that_owns_the_project(self) -> None:
        root_a, _, a = self._harness("alpha", "ta")
        root_b, _, b = self._harness("beta", "tb")
        aggregator, base = self._web([a, b])
        status, _ = self._post(base, "/api/chat-model", {"project": "beta", "provider": "codex", "model": "gpt-6-luna"})
        self.assertEqual(status, 200)
        self.assertEqual(read_model_settings(models_file(root_b))["HARNESS_CHAT_MODEL"], "gpt-6-luna")
        self.assertEqual(read_model_settings(models_file(root_a))["HARNESS_CHAT_MODEL"], "haiku")  # untouched

    def test_errors_from_the_harness_come_back_as_the_pages_error_text(self) -> None:
        _, _, a = self._harness("alpha", "ta")
        _, base = self._web([a])
        status, body = self._post(base, "/api/chat-model", {"project": "alpha", "provider": "openai", "model": "x"})
        self.assertEqual(status, 400)
        self.assertIn("unknown provider", body["error"])
        self.assertEqual(self._post(base, "/api/chat-model", {"provider": "claude"})[0], 400)  # no project
        self.assertEqual(self._post(base, "/api/not-a-thing", {"project": "alpha"})[0], 404)

    def test_an_unknown_project_is_refused_when_there_are_several_harnesses(self) -> None:
        _, _, a = self._harness("alpha", "ta")
        _, _, b = self._harness("beta", "tb")
        _, base = self._web([a, b])
        status, body = self._post(base, "/api/chat-model", {"project": "nope", "provider": "claude", "model": "x"})
        self.assertEqual(status, 400)
        self.assertIn("no Harness serves", body["error"])

    def test_preferences_carry_their_harness_so_ids_never_collide(self) -> None:
        _, pa, a = self._harness("alpha", "ta")
        _, pb, b = self._harness("beta", "tb")
        for parts in (pa, pb):
            parts.trace_store.add_preference_candidate(
                project=parts.scope.name, scope="project", category="workflow", text="先給結論", evidence=[],
                explicit=True, status="adopted", source_digest_date="2026-10-01", adopted_by="operator",
            )
        aggregator, _ = self._web([a, b])
        adopted = aggregator.collect()["preferences"]["adopted"]
        self.assertEqual(len({r["uid"] for r in adopted}), len(adopted))
        self.assertTrue(all(r["uid"].startswith(r["harness"] + ":") for r in adopted))

    def test_a_save_is_visible_at_once_not_after_the_cached_answer_expires(self) -> None:
        # The POST looks up the project's Harness (which caches the page's data for a second), then
        # announces the change; what the page is pushed next must already show the save.
        import os
        archive_settings = patch.dict(os.environ, {"HARNESS_ARCHIVE_PROVIDER": "", "HARNESS_ARCHIVE_MODEL": "", "HARNESS_ARCHIVE_FALLBACKS": ""})
        archive_settings.start()
        self.addCleanup(archive_settings.stop)
        _, _, a = self._harness("alpha", "ta")
        aggregator, base = self._web([a])
        before = aggregator.collect()["projects"][0]
        self.assertFalse(before["archive_is_own"])

        status, _ = self._post(base, "/api/archive-chain", {
            "project": "alpha",
            "chain": [{"provider": "claude", "model": "opus"}, {"provider": "codex", "model": "gpt-5.6-sol"}],
        })

        self.assertEqual(status, 200)
        after = aggregator.collect()["projects"][0]
        self.assertTrue(after["archive_is_own"])
        self.assertEqual([c["model"] for c in after["archive_candidates"]], ["opus", "gpt-5.6-sol"])

    def test_the_config_lists_the_harnesses(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.toml"
            path.write_text(
                'host = "127.0.0.1"\nport = 9000\n\n[[harness]]\nname = "a"\nurl = "http://127.0.0.1:1"\ntoken = "t"\n',
                encoding="utf-8",
            )
            host, port, targets = load_config(path)
            self.assertEqual((host, port, [t.name for t in targets]), ("127.0.0.1", 9000, ["a"]))
            path.write_text('port = 9000\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
