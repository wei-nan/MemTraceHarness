from unittest import TestCase
from datetime import datetime, timezone
from unittest.mock import patch

from memtrace_harness.improvement import ImprovementStore, update_settings
from memtrace_services.observation import ObservationRelay
from tests.test_api import ApiTestCase
from tests import test_web_service as web_helpers


class ObservationApiTests(ApiTestCase):
    def test_observations_are_operator_only_opt_in_and_source_scoped(self):
        self.api.peer_token = "peer-token"
        for method, path, body in (("GET", "/v1/observations", None), ("GET", "/v1/improvement", None), ("POST", "/v1/observations", {})):
            self.assertEqual(self._raw(method, path, body, token=None)[0], 401)
            self.assertEqual(self._raw(method, path, body, token="peer-token")[0], 401)
        self.assertFalse(self.client._request("GET", "/v1/observations")["sharing"])
        self.client.admin("improvement", dict(enabled=True, auto_develop=False, sources=["Beri"], share_enabled=True))
        result = self.client._request("POST", "/v1/observations", dict(source="Beri", cursor=1, events=[dict(id="turn:1", kind="conversation", text="新的對話內容，附上實際處理結果。", at=datetime.now(timezone.utc).isoformat())]))
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(self._raw("POST", "/v1/observations", dict(source="unknown"))[0], 400)
        self.assertEqual(self._raw("GET", "/v1/observations?after=invalid")[0], 400)
        self.assertEqual(self._raw("POST", "/v1/admin/improvement", dict(enabled="true"))[0], 400)
        self.assertEqual(self.client._request("GET", "/v1/improvement")["pending_evidence"], 1)
        self.assertEqual(self.parts.trace_store.list_queued_tasks(self.scope.workspace_id), [])


class ObservationRelayTests(TestCase):
    _harness = web_helpers.WebServiceTests._harness

    def test_opt_in_api_relay_and_durable_restart_cursor(self):
        with patch("memtrace_harness.cli_process.CliProcessRunner.run") as cli:
            _, coordinator, target = self._harness("platform", "cp")
            _, source, source_target = self._harness("Beri", "st")
            update_settings(coordinator.config, dict(enabled=True, sources=["Beri", "missing"]))
            source.primary_session_mgr.record_turn(project="Beri", speaker="user", turn_type="chat", content="幫我檢查排程，這次又沒有執行。")
            relay = ObservationRelay([target, source_target])
            relay.collect_once()
            store = ImprovementStore(coordinator.trace_store)
            state = store.public(coordinator.config)
            self.assertEqual(state["pending_evidence"], 0)
            self.assertEqual({s["source"]: s["status"] for s in state["sources_state"]}, dict(Beri="sharing_disabled", missing="unreachable"))
            update_settings(source.config, dict(share_enabled=True))
            with coordinator.trace_store._connection() as conn:
                conn.execute("UPDATE observation_sources SET collected_at=0")
            relay.collect_once()
            state = store.public(coordinator.config)
            self.assertEqual(state["pending_evidence"], 1)
            first_cursor = next(s["cursor"] for s in state["sources_state"] if s["source"] == "Beri")
            source.primary_session_mgr.record_turn(project="Beri", speaker="assistant", turn_type="chat", content="這是接續的結果，並沒有重跑前面的工作。")
            with coordinator.trace_store._connection() as conn:
                conn.execute("UPDATE observation_sources SET collected_at=0")
            ObservationRelay([target, source_target]).collect_once()
            state = store.public(coordinator.config)
            self.assertEqual(state["pending_evidence"], 2)
            self.assertGreater(next(s["cursor"] for s in state["sources_state"] if s["source"] == "Beri"), first_cursor)
            # Turning the coordinator off stops further collection without changing source settings.
            update_settings(coordinator.config, dict(enabled=False))
            relay.collect_once()
            self.assertEqual(store.public(coordinator.config)["pending_evidence"], 2)
            cli.assert_not_called()
