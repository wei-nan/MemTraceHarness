"""Legacy link migration, stable Harness labels, and live authorization refresh.
New contact approval and website flows are covered in test_contacts.py.
"""

import json
import os
import stat
import urllib.error
import urllib.request
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness import admin
from memtrace_harness.api import HarnessApi
from memtrace_harness.peer import queue_outbound
from memtrace_harness.peer_links import LinkError, PeerLinks, links_path, sanitize_name
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from memtrace_services.a2a.carrier import Carrier
from memtrace_services.a2a.client import PeerClient
from memtrace_services.a2a.config import A2aConfig, LiveConfig
from memtrace_services.a2a.gateway import PeerGateway
from memtrace_services.a2a.protocol import Dispatcher
from memtrace_services.a2a.server import start_server as start_a2a
from memtrace_services.client import HarnessError
from memtrace_services.web.aggregate import Aggregator, HarnessTarget
from memtrace_services.web.chat import ChatHub
from memtrace_services.web.server import start_server as start_web
from tests.test_peer_api import reply
from tests.test_peer_calling import free_port
from tests.workspace_support import workspace


class LinkStoreTests(TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.links = PeerLinks(Path(self.tmp.name) / ".harness" / "peer_links.json")

    def test_a_token_exists_in_clear_only_when_it_is_made(self) -> None:
        token = self.links.create_inbound("beri", "ask")
        self.assertNotIn(token, self.links.path.read_text())
        self.assertEqual(self.links.snapshot_for_service()["inbound"][0]["name"], "beri")
        self.assertEqual(len(self.links.snapshot_for_service()["inbound"][0]["token_sha256"]), 64)
        self.assertEqual(stat.S_IMODE(os.stat(self.links.path).st_mode), 0o600)

    def test_making_a_link_again_replaces_its_token(self) -> None:
        first, second = self.links.create_inbound("beri", "ask"), self.links.create_inbound("beri", "collaborate")
        self.assertNotEqual(first, second)
        view = self.links.public_view()["inbound"]
        self.assertEqual([(v["name"], v["trust"]) for v in view], [("beri", "collaborate")])

    def test_the_public_view_has_no_secret_but_the_service_view_has_them(self) -> None:
        self.links.create_inbound("beri", "ask")
        self.links.set_outbound("platform", "http://127.0.0.1:8900/", "secret-out-token", True)
        public = json.dumps(self.links.public_view())
        for secret in ("secret-out-token", "token_sha256", "token"):
            self.assertNotIn(secret, public)
        service = self.links.snapshot_for_service()
        self.assertEqual(service["outbound"], [{"name": "platform", "display_name": "platform", "url": "http://127.0.0.1:8900", "token": "secret-out-token", "collaborate": True}])

    def test_changes_and_revocation(self) -> None:
        self.links.create_inbound("beri", "ask")
        self.links.set_inbound_trust("beri", "collaborate")
        self.assertEqual(self.links.public_view()["inbound"][0]["trust"], "collaborate")
        self.assertTrue(self.links.revoke_inbound("beri"))
        self.assertFalse(self.links.revoke_inbound("beri"))
        self.links.set_outbound("p", "https://h.example:9000", "t", False)
        self.links.set_outbound_collaborate("p", True)
        self.assertTrue(self.links.public_view()["outbound"][0]["collaborate"])
        self.assertTrue(self.links.remove_outbound("p"))
        with self.assertRaises(LinkError):
            self.links.set_inbound_trust("ghost", "ask")

    def test_bad_input_is_refused(self) -> None:
        for call in (
            lambda: self.links.create_inbound("Bad Name", "ask"),
            lambda: self.links.create_inbound("ok", "root"),
            lambda: self.links.set_outbound("p", "ftp://x", "t", False),
            lambda: self.links.set_outbound("p", "http://x:1", "has space", False),
            lambda: self.links.set_outbound("p", "http://x:1", "", False),
            lambda: self.links.set_endpoint("javascript:alert(1)"),
        ):
            with self.assertRaises(LinkError):
                call()

    def test_a_project_name_becomes_a_peer_name(self) -> None:
        self.assertEqual(sanitize_name("TWTradingStrategy"), "twtradingstrategy")
        self.assertEqual(sanitize_name(" My Project! "), "my-project")
        with self.assertRaises(LinkError):
            sanitize_name("專案")


class Harnesses:
    """Two real Harnesses, each with its A2A service (reading links live), and the status page over both."""

    def __init__(self, case: TestCase, names=("alpha", "beta"), with_a2a=("alpha", "beta")) -> None:
        self.case, self.h = case, {}
        for name in names:
            self.h[name] = self._harness(name)
        for name in with_a2a:
            self._a2a(name)
        targets = [self.h[n]["target"] for n in names]
        self.aggregator = Aggregator(targets)
        self.aggregator.collect()  # the first status of a Harness is slow (warm-up); keep that out of the requests under test
        web = start_web("127.0.0.1", 0, self.aggregator, ChatHub([]))
        case.addCleanup(web.server_close)
        case.addCleanup(web.shutdown)
        self.web = f"http://127.0.0.1:{web.server_address[1]}"

    def _harness(self, name: str) -> dict:
        ctx = workspace({
            "HARNESS_API_TOKEN": f"main-{name}", "HARNESS_PEER_API_TOKEN": f"peer-{name}",
            "HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku",
        })
        root, config = ctx.__enter__()
        self.case.addCleanup(ctx.__exit__, None, None, None)
        config = replace(config, api_port=0)
        scope = replace(ProjectScope.from_file(root / "harness-scope.md"), name=name)
        parts = build_harness(config, scope)
        api = HarnessApi(parts.service, config, scope)
        server = api.start()
        self.case.addCleanup(api.stop)
        self.case.addCleanup(server.server_close)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        return {
            "root": root, "config": config, "scope": scope, "service": parts.service, "api": api, "url": url,
            "store": parts.service.approval_manager.trace_store, "gateway": PeerGateway(url, f"peer-{name}"),
            "target": HarnessTarget(name, url, f"main-{name}"),
        }

    def _a2a(self, name: str) -> None:
        h = self.h[name]
        port = free_port()
        base = A2aConfig(name=name, description="d", host="127.0.0.1", port=port, public_url=f"http://127.0.0.1:{port}",
                         harness_url=h["url"], harness_peer_token=f"peer-{name}")
        live = LiveConfig(base, h["gateway"], ttl=0)
        server = start_a2a(live, Dispatcher(live, h["gateway"], block_seconds=5, poll_seconds=0.02))
        self.case.addCleanup(server.server_close)
        self.case.addCleanup(server.shutdown)
        h["live"] = live
        h["carrier"] = Carrier(live, h["gateway"], client_factory=lambda peer: PeerClient(peer, poll_seconds=0.02, timeout=3, collaborate_poll_seconds=0.02))
        h["carrier"].sync_directory()  # tells the Harness this service's address

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.web + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else {}

    def links(self, name: str) -> dict:
        return PeerLinks(links_path(self.h[name]["config"])).public_view()


class LiveConfigTests(TestCase):
    class Gateway:
        def __init__(self) -> None:
            self.reply: dict | Exception = {"inbound": [], "outbound": []}
            self.calls = 0

        def links(self):
            self.calls += 1
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

    def base(self, **extra):
        from memtrace_services.a2a.config import OutboundPeer, PeerEntry, hash_token

        return A2aConfig(
            name="n", description="d", host="x", port=1, public_url="http://x", harness_url="h", harness_peer_token="t",
            peers={"filed": PeerEntry("filed", hash_token("file-token"))},
            outbound={"filed-out": OutboundPeer("filed-out", "http://f", "ft")}, **extra,
        )

    def test_the_harnesss_links_join_the_files_and_win_on_a_shared_name(self) -> None:
        from memtrace_services.a2a.config import hash_token

        gateway = self.Gateway()
        gateway.reply = {
            "inbound": [{"name": "page", "token_sha256": hash_token("page-token"), "trust": "collaborate"},
                        {"name": "filed", "token_sha256": hash_token("new-token"), "trust": "ask"}],
            "outbound": [{"name": "o", "url": "http://o/", "token": "ot", "collaborate": True}],
        }
        live = LiveConfig(self.base(), gateway, ttl=0)
        self.assertEqual(live.authenticate("page-token").skills, ("ask", "collaborate"))
        self.assertIsNone(live.authenticate("file-token"))  # replaced on the page: the old file token is dead
        self.assertEqual(live.authenticate("new-token").name, "filed")
        self.assertEqual(sorted(live.outbound), ["filed-out", "o"])
        self.assertEqual(live.outbound["o"].url, "http://o")
        self.assertEqual(live.name, "n")  # the file's fixed fields pass through

    def test_a_revocation_is_seen_on_the_next_read_and_a_dead_harness_keeps_the_last_links(self) -> None:
        from memtrace_services.a2a.config import hash_token

        gateway = self.Gateway()
        gateway.reply = {"inbound": [{"name": "page", "token_sha256": hash_token("page-token"), "trust": "ask"}], "outbound": []}
        live = LiveConfig(self.base(), gateway, ttl=0)
        self.assertIsNotNone(live.authenticate("page-token"))
        gateway.reply = {"inbound": [], "outbound": []}
        self.assertIsNone(live.authenticate("page-token"))
        gateway.reply = {"inbound": [{"name": "page", "token_sha256": hash_token("page-token"), "trust": "ask"}], "outbound": []}
        self.assertIsNotNone(live.authenticate("page-token"))
        gateway.reply = RuntimeError("down")
        self.assertIsNone(live.authenticate("page-token"))  # unavailable authority cannot prove an unrevoked grant
        self.assertIn("page", live.peers)  # discovery can still show the cached entry

    def test_links_are_cached_for_the_ttl(self) -> None:
        gateway = self.Gateway()
        now = [0.0]
        live = LiveConfig(self.base(), gateway, ttl=10, clock=lambda: now[0])
        live.peers, live.peers  # noqa: B018
        self.assertEqual(gateway.calls, 1)
        now[0] = 11
        live.peers  # noqa: B018
        self.assertEqual(gateway.calls, 2)


class HarnessNameTests(TestCase):
    """A Harness is called by its own name, not by its project's or folder's."""

    def test_the_name_set_for_a_harness_wins_and_a_bad_one_falls_back(self) -> None:
        from types import SimpleNamespace

        from memtrace_harness.peer_links import harness_name

        scope = SimpleNamespace(name="Beri")
        self.assertEqual(harness_name(SimpleNamespace(peer_name=None), scope), "beri")  # the project's name, lowercased
        self.assertEqual(harness_name(SimpleNamespace(peer_name="beri-app"), scope), "beri-app")
        self.assertEqual(harness_name(SimpleNamespace(peer_name="Bad Name!"), scope), "beri")
        self.assertEqual(harness_name(SimpleNamespace(peer_name=None), SimpleNamespace(name="專案")), "harness")
        with patch.dict(os.environ, {"HARNESS_PEER_NAME": "from-env"}):  # read at call time: a rename shows at once
            self.assertEqual(harness_name(SimpleNamespace(peer_name="old"), scope), "from-env")

    def test_a_rename_is_validated_persisted_and_shown(self) -> None:
        from memtrace_harness.workspace import models_file, read_model_settings

        w = Harnesses(self, names=("alpha",), with_a2a=("alpha",))
        h = w.h["alpha"]
        admin.update_peer_name(h["config"], "alpha-app")
        self.assertEqual(read_model_settings(models_file(h["root"]))["HARNESS_PEER_NAME"], "alpha-app")
        self.assertEqual(h["gateway"].profile()["name"], "alpha-app")
        self.assertEqual(admin.collect_status_data(h["config"], h["scope"])["projects"][0]["peer_name"], "alpha-app")
        for bad in ("", "Has Space", "UPPER", "x" * 33, "專案", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                admin.update_peer_name(h["config"], bad)

    def test_the_agent_card_carries_the_harnesss_name_and_falls_back_to_the_file(self) -> None:
        from memtrace_services.a2a.protocol import agent_card

        base = A2aConfig(name="from-file", description="d", host="x", port=1, public_url="http://x",
                         harness_url="h", harness_peer_token="t")
        self.assertEqual(agent_card(base)["name"], "from-file")
        self.assertEqual(agent_card(base, None, "alpha-app")["name"], "alpha-app")
        self.assertEqual(agent_card(base, None, "  ")["name"], "from-file")
