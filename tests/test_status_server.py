from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig, project_role_profiles_env_var
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.status_server import start_status_server
from memtrace_harness.telegram_gateway import TelegramGateway
from memtrace_harness.trace_store import TraceStore

CHAT_ID = 999111


class StatusServerWriteEndpointTests(TestCase):
    """Live end-to-end tests against a real ThreadingHTTPServer (bound to an
    OS-assigned free port on 127.0.0.1) for the dashboard's write endpoints —
    exercising the actual HTTP layer, not just the underlying cli.py helpers
    (already covered directly in test_cli.py)."""

    def _start(self, tmp_path: Path, *, allowed_chat_ids: set[int] | None = None):
        db_path = tmp_path / "trace.sqlite3"
        trace_store = TraceStore(db_path)
        config = HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None,
            trace_db_path=db_path, trace_root=tmp_path,
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900,
            telegram_bot_token=None,
            telegram_allowed_chat_ids=allowed_chat_ids or {CHAT_ID},
            project_index_path=None, chat_provider="claude", chat_model="haiku",
            unattended_write_requires_approval=True,
            status_server_host="127.0.0.1", status_server_port=0,
        )
        approval_mgr = ApprovalManager(trace_store, config.telegram_allowed_chat_ids)
        scope = ProjectScope(
            name="TestProj", workspace_id="ws_test",
            working_directory=tmp_path, scope_file_path=tmp_path / "harness-scope.md",
        )
        triage = ChatTriage([scope])
        psess_mgr = PrimarySessionManager(trace_store)
        gateway = TelegramGateway(config, approval_mgr, triage, psess_mgr, [scope])

        server, bus = start_status_server(config, {"TestProj": gateway})
        assert server is not None and bus is not None
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        return server, port, trace_store, approval_mgr, scope, config

    def _post(self, port: int, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_reject_via_dashboard_resolves_the_same_way_telegram_would(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            _server, port, trace_store, approval_mgr, scope, _config = self._start(tmp_path)

            trace_store.acquire_workspace_lock(scope.workspace_id, "conv_1")
            req = approval_mgr.request_approval(
                conversation_id="conv_1",
                workspace=scope.workspace_id,
                working_directory=str(tmp_path),
                reason="ambiguous_requirement",
                proposed_action="check something",
            )
            approval_mgr.record_telegram_message(req.id, chat_id=CHAT_ID, message_id=1)

            status, body = self._post(
                port, "/api/approval",
                {"project": "TestProj", "request_id": req.id, "action": "reject"},
            )

            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            updated = approval_mgr.get_request(req.id)
            self.assertEqual(updated.status, "rejected")
            # Rejecting releases the workspace lock, same as the Telegram path.
            self.assertIsNone(trace_store.get_workspace_lock(scope.workspace_id))

    def test_approval_action_rejects_unknown_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            _server, port, _trace_store, _approval_mgr, _scope, _config = self._start(tmp_path)

            status, body = self._post(
                port, "/api/approval",
                {"project": "NoSuchProject", "request_id": "appr_x", "action": "reject"},
            )
            self.assertEqual(status, 400)
            self.assertIn("no gateway registered", body["error"])

    def test_approval_action_rejects_bad_action_value(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            _server, port, _trace_store, _approval_mgr, _scope, _config = self._start(tmp_path)

            status, body = self._post(
                port, "/api/approval",
                {"project": "TestProj", "request_id": "appr_x", "action": "delete_everything"},
            )
            self.assertEqual(status, 400)
            self.assertIn("unsupported action", body["error"])

    def test_make_independent_endpoint_creates_file_and_env_entry(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # create_dedicated_role_profiles_file() writes to bare relative
            # "profiles/"/".env" paths — isolate the whole test in a temp cwd so it
            # can never touch this repo's real profiles/ or .env.
            original_cwd = os.getcwd()
            os.chdir(tmp_path)
            self.addCleanup(os.chdir, original_cwd)

            index_path = tmp_path / "projects.index.txt"
            scope_dir = tmp_path / "TestProj"
            scope_dir.mkdir()
            scope_path = scope_dir / "harness-scope.md"
            scope_path.write_text(
                "# Harness scope — TestProj\n\n- workspace_id: ws_test\n", encoding="utf-8"
            )
            index_path.write_text(str(scope_path) + "\n", encoding="utf-8")

            db_path = tmp_path / "trace.sqlite3"
            config = HarnessConfig(
                memtrace_mcp_url=None, memtrace_api_token=None,
                trace_db_path=db_path, trace_root=tmp_path,
                claude_command="claude", codex_command="codex", antigravity_command="agy",
                antigravity_output_mode="auto", cli_timeout_seconds=900,
                telegram_bot_token=None, telegram_allowed_chat_ids={CHAT_ID},
                project_index_path=index_path, chat_provider="claude", chat_model="haiku",
                unattended_write_requires_approval=True,
                status_server_host="127.0.0.1", status_server_port=0,
            )
            env_var = project_role_profiles_env_var("TestProj")
            os.environ.pop(env_var, None)
            self.addCleanup(os.environ.pop, env_var, None)

            server, bus = start_status_server(config, {})
            assert server is not None and bus is not None
            self.addCleanup(server.shutdown)
            self.addCleanup(server.server_close)
            port = server.server_address[1]

            status, body = self._post(port, "/api/role-profile/independent", {"project": "TestProj"})

            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            dest = Path(body["path"])
            self.assertTrue(dest.is_file())
            self.assertIn(f"{env_var}={dest}", (tmp_path / ".env").read_text(encoding="utf-8"))
