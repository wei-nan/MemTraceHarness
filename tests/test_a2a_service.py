"""The A2A service in front of a real Harness API: a peer's token, the JSON-RPC surface, the Agent Card."""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from unittest.mock import patch

from memtrace_services.a2a.config import A2aConfig, PeerEntry, hash_token, load_config, new_peer_token
from memtrace_services.a2a.gateway import PeerGateway
from memtrace_services.a2a.protocol import HOPS_KEY, Dispatcher
from memtrace_services.a2a.server import RateLimiter, start_server
from tests.test_peer_api import PEER_TOKEN, PeerApiCase, reply

BERI_TOKEN = "beri-secret-token"
OTHER_TOKEN = "other-secret-token"


class A2aCase(PeerApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = A2aConfig(
            name="TestProj", description="d", host="127.0.0.1", port=0, public_url="http://127.0.0.1:0",
            harness_url=self.url, harness_peer_token=PEER_TOKEN,
            peers={
                "beri": PeerEntry("beri", hash_token(BERI_TOKEN), max_concurrent=2, rate_per_min=100),
                "other": PeerEntry("other", hash_token(OTHER_TOKEN), max_concurrent=2, rate_per_min=100),
            },
        )
        self.limiter = RateLimiter()
        dispatcher = Dispatcher(self.cfg, PeerGateway(self.url, PEER_TOKEN), block_seconds=5, poll_seconds=0.02)
        self.a2a = start_server(self.cfg, dispatcher, self.limiter)
        self.addCleanup(self.a2a.server_close)
        self.addCleanup(self.a2a.shutdown)
        self.a2a_url = f"http://127.0.0.1:{self.a2a.server_address[1]}"

    def rpc(self, method, params=None, *, token=BERI_TOKEN, version="1.0", rid=1):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if version:
            headers["A2A-Version"] = version
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        request = urllib.request.Request(self.a2a_url + "/rpc", data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def send(self, text="進度如何？", **extra):
        message = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}], **extra}
        return self.rpc("SendMessage", {"message": message})


class CardTests(A2aCase):
    def test_the_agent_card_is_public_and_reveals_nothing_private(self) -> None:
        with urllib.request.urlopen(self.a2a_url + "/.well-known/agent-card.json", timeout=5) as resp:
            card = json.loads(resp.read())
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertEqual([s["id"] for s in card["skills"]], ["ask", "collaborate"])  # what exists; who may use it is not public
        self.assertEqual(card["capabilities"], {"streaming": False, "pushNotifications": False, "extendedAgentCard": False})
        dumped = json.dumps(card)
        for secret in ("beri", "other", PEER_TOKEN, hash_token(BERI_TOKEN), str(self.root)):
            self.assertNotIn(secret, dumped)


class AuthTests(A2aCase):
    def test_no_token_or_a_wrong_token_is_401_and_alike(self) -> None:
        self.assertEqual(self.send()[0], 200)
        for token in (None, "nope", ""):
            self.assertEqual(self.rpc("GetTask", {"id": "x"}, token=token)[0], 401)

    def test_the_harness_main_token_is_not_a_peer_token(self) -> None:
        from tests.test_api import TOKEN
        self.assertEqual(self.rpc("GetTask", {"id": "x"}, token=TOKEN)[0], 401)

    def test_an_unsupported_version_is_refused(self) -> None:
        for version in (None, "0.3", "2.0"):
            _, body = self.rpc("GetTask", {"id": "x"}, version=version)
            self.assertEqual(body["error"]["code"], -32009)

    def test_the_rate_limit_is_429(self) -> None:
        self.cfg.peers["beri"] = PeerEntry("beri", hash_token(BERI_TOKEN), rate_per_min=2)
        self.assertEqual([self.rpc("ListTasks")[0] for _ in range(3)], [200, 200, 429])
        self.assertEqual(self.rpc("ListTasks", token=OTHER_TOKEN)[0], 200)  # another peer has its own budget

    def test_task_polling_and_cancellation_have_separate_bounded_budgets(self) -> None:
        self.cfg.peers["beri"] = PeerEntry("beri", hash_token(BERI_TOKEN), rate_per_min=2)
        self.assertEqual([self.rpc("ListTasks")[0] for _ in range(3)], [200, 200, 429])
        # Polling an accepted task still works after the request budget is exhausted.
        for _ in range(15):
            status, body = self.rpc("GetTask", {"id": "unknown"})
            self.assertEqual((status, body["error"]["code"]), (200, -32001))
        for _ in range(105):
            self.assertTrue(self.limiter.allow("beri", 120, budget="task-status"))
        self.assertEqual(self.rpc("GetTask", {"id": "unknown"})[0], 429)
        # A saturated poller cannot prevent cancellation, nor remove the other peer's budget.
        self.assertEqual(self.rpc("CancelTask", {"id": "unknown"})[0], 200)
        self.assertEqual(self.rpc("GetTask", {"id": "unknown"}, token=OTHER_TOKEN)[0], 200)

    def test_a_bad_body_is_a_json_rpc_error_not_a_crash(self) -> None:
        request = urllib.request.Request(
            self.a2a_url + "/rpc", data=b"{not json",
            headers={"Authorization": f"Bearer {BERI_TOKEN}", "A2A-Version": "1.0"},
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["error"]["code"], -32700)
        self.assertEqual(self.rpc("Nope")[1]["error"]["code"], -32601)


class SendMessageTests(A2aCase):
    def test_a_blocking_send_returns_the_completed_task_with_the_answer(self) -> None:
        with patch("memtrace_harness.peer.CliProcessRunner.run", return_value=reply("做完了 A。")):
            status, body = self.send(contextId="ctx-7")
        task = body["result"]["task"]
        self.assertEqual((status, task["status"]["state"], task["contextId"]), (200, "TASK_STATE_COMPLETED", "ctx-7"))
        self.assertEqual(task["artifacts"][0]["parts"], [{"text": "做完了 A。"}])

    def test_return_immediately_gives_back_a_task_to_poll(self) -> None:
        release = threading.Event()

        def slow(*a, **k):
            release.wait(5)
            return reply("later")

        with patch("memtrace_harness.peer.CliProcessRunner.run", side_effect=slow):
            message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "q"}]}
            _, body = self.rpc("SendMessage", {"message": message, "configuration": {"returnImmediately": True}})
            task = body["result"]["task"]
            self.assertIn(task["status"]["state"], ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"))
            release.set()
            for _ in range(200):
                got = self.rpc("GetTask", {"id": task["id"]})[1]["result"]
                if got["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                threading.Event().wait(0.02)
        self.assertEqual(got["artifacts"][0]["parts"][0]["text"], "later")

    def test_a_non_text_part_is_refused(self) -> None:
        message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"url": "http://x/y.png"}]}
        self.assertEqual(self.rpc("SendMessage", {"message": message})[1]["error"]["code"], -32005)
        self.assertEqual(self.rpc("SendMessage", {"message": {"parts": []}})[1]["error"]["code"], -32602)

    def test_the_hop_count_travels_and_the_limit_rejects_the_task(self) -> None:
        _, body = self.send(metadata={HOPS_KEY: 2})
        task = body["result"]["task"]
        self.assertEqual(task["status"]["state"], "TASK_STATE_REJECTED")
        self.assertEqual(task["status"]["message"]["parts"], [{"text": "too many hops"}])
        self.assertEqual(self.send(metadata={HOPS_KEY: "x"})[1]["error"]["code"], -32602)

    def test_a_skill_the_peer_is_not_allowed_is_refused_before_the_harness_sees_it(self) -> None:
        _, body = self.send(metadata={"memtrace.harness/skill": "delegate"})
        self.assertEqual(body["error"]["code"], -32004)
        self.assertEqual(self.service.approval_manager.trace_store.list_peer_requests("beri"), [])

    def test_continuing_a_task_is_not_offered(self) -> None:
        with patch("memtrace_harness.peer.CliProcessRunner.run", return_value=reply("x")):
            task = self.send()[1]["result"]["task"]
        self.assertEqual(self.send(taskId=task["id"])[1]["error"]["code"], -32004)
        self.assertEqual(self.send(taskId="peer_nope")[1]["error"]["code"], -32001)

    def test_a_peer_over_its_concurrency_budget_is_refused(self) -> None:
        release = threading.Event()

        def slow(*a, **k):
            release.wait(5)
            return reply("x")

        with patch("memtrace_harness.peer.CliProcessRunner.run", side_effect=slow):
            for _ in range(2):
                message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "q"}]}
                self.rpc("SendMessage", {"message": message, "configuration": {"returnImmediately": True}})
            code = self.send()[1]["error"]["code"]
            release.set()
        self.assertEqual(code, -32010)


class TaskAccessTests(A2aCase):
    def test_get_list_cancel_are_scoped_to_the_calling_peer(self) -> None:
        with patch("memtrace_harness.peer.CliProcessRunner.run", return_value=reply("mine")):
            task = self.send()[1]["result"]["task"]
        self.assertEqual(self.rpc("GetTask", {"id": task["id"]})[1]["result"]["id"], task["id"])
        self.assertEqual(len(self.rpc("ListTasks")[1]["result"]["tasks"]), 1)
        theirs = self.rpc("GetTask", {"id": task["id"]}, token=OTHER_TOKEN)[1]
        missing = self.rpc("GetTask", {"id": "peer_nope"}, token=OTHER_TOKEN)[1]
        self.assertEqual(theirs, missing)
        self.assertEqual(self.rpc("ListTasks", token=OTHER_TOKEN)[1]["result"]["tasks"], [])
        self.assertEqual(self.rpc("CancelTask", {"id": task["id"]}, token=OTHER_TOKEN)[1]["error"]["code"], -32001)

    def test_a_finished_task_cannot_be_canceled(self) -> None:
        with patch("memtrace_harness.peer.CliProcessRunner.run", return_value=reply("done")):
            task = self.send()[1]["result"]["task"]
        self.assertEqual(self.rpc("CancelTask", {"id": task["id"]})[1]["error"]["code"], -32002)

    def test_streaming_push_and_extended_card_are_declared_unsupported(self) -> None:
        for method, code in (("SendStreamingMessage", -32004), ("SubscribeToTask", -32004),
                             ("GetExtendedAgentCard", -32004),
                             ("CreateTaskPushNotificationConfig", -32003), ("ListTaskPushNotificationConfigs", -32003)):
            with self.subTest(method=method):
                self.assertEqual(self.rpc(method)[1]["error"]["code"], code)

    def test_the_harness_being_down_is_a_plain_error_that_leaks_nothing(self) -> None:
        self.cfg_down = Dispatcher(self.cfg, PeerGateway("http://127.0.0.1:1", PEER_TOKEN))
        out = self.cfg_down.handle(self.cfg.peers["beri"], {"jsonrpc": "2.0", "id": 1, "method": "ListTasks"})
        self.assertEqual(out["error"], {"code": -32603, "message": "temporarily unavailable"})


class ConfigTests(A2aCase):
    def test_a_config_file_round_trips_and_checks_its_hashes(self) -> None:
        token, digest = new_peer_token()
        path = self.root / "a2a.toml"
        path.write_text(
            f'[harness]\nurl = "http://127.0.0.1:1"\npeer_token = "t"\n[peers.beri]\ntoken_sha256 = "{digest}"\n', encoding="utf-8"
        )
        config = load_config(path)
        self.assertEqual(config.authenticate(token).name, "beri")
        self.assertIsNone(config.authenticate(token + "x"))
        self.assertIsNone(config.authenticate(""))
        path.write_text('[harness]\nurl = "u"\npeer_token = "t"\n[peers.x]\ntoken_sha256 = "short"\n', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_config(path)
