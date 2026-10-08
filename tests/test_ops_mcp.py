from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

from memtrace_harness.ops_mcp import OpsContext, OpsError, call_tool, handle_message, http_probe, ops_server_spec
from memtrace_harness.scope import ProjectScope
from memtrace_harness.trace_store import TraceStore


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.headers, self._body = status, {}, body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class OpsToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logs").mkdir()
        (self.tmp / "logs" / "job.log").write_text("\n".join(f"line {i}" for i in range(500)))
        self.store = TraceStore(self.tmp / "trace.db")
        self.ctx = OpsContext(
            project="P", working_directory=self.tmp, hosts=["www.twse.com.tw"],
            logs=["logs/job.log"], jobs={"watchlist": "echo hi"}, store=self.store,
        )

    def test_probe_refuses_hosts_the_operator_did_not_list(self) -> None:
        with self.assertRaises(OpsError):
            http_probe(self.ctx, {"url": "https://evil.example/x"})
        with self.assertRaises(OpsError):
            http_probe(self.ctx, {"url": "file:///etc/passwd"})

    def test_probe_reports_each_attempt_with_status_timing_and_errors(self) -> None:
        answers = iter([_Response(200, b'{"stat":"OK"}'), urllib.error.URLError(TimeoutError("timed out"))])

        def opener(request: object, timeout: int) -> _Response:
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        text = http_probe(
            self.ctx, {"url": "https://www.twse.com.tw/x", "attempts": "2"}, opener=opener, sleep=lambda _: None
        )
        self.assertIn("#1: HTTP 200", text)
        self.assertIn('{"stat":"OK"}', text)
        self.assertIn("#2: TimeoutError: timed out", text)

    def test_read_log_returns_only_the_tail_of_a_declared_file(self) -> None:
        text = call_tool(self.ctx, "read_log", {"name": "job.log", "lines": "3"})
        self.assertTrue(text.rstrip().endswith("line 497\nline 498\nline 499"))
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "read_log", {"name": "../../etc/passwd"})

    def test_run_job_only_records_a_request_and_never_runs_it(self) -> None:
        text = call_tool(self.ctx, "run_job", {"name": "watchlist"})
        self.assertIn("confirm", text)
        pending = self.store.claim_pending_job_requests(["P"])
        self.assertEqual([(j["job_name"], j["command"]) for j in pending], [("watchlist", "echo hi")])
        with self.assertRaises(OpsError):
            call_tool(self.ctx, "run_job", {"name": "rm_everything"})

    def test_a_job_already_waiting_is_not_requested_twice(self) -> None:
        call_tool(self.ctx, "run_job", {"name": "watchlist"})
        call_tool(self.ctx, "run_job", {"name": "watchlist"})
        self.assertEqual(len(self.store.claim_pending_job_requests(["P"])), 1)

    def test_only_declared_capabilities_are_listed(self) -> None:
        ctx = OpsContext(project="P", working_directory=self.tmp, hosts=[], logs=[], jobs={})
        reply = handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, ctx)
        self.assertEqual(reply["result"]["tools"], [])
        reply = handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "run_job", "arguments": {"name": "x"}}},
            ctx,
        )
        self.assertTrue(reply["result"]["isError"])


class ScopeAndSpecTests(unittest.TestCase):
    def test_scope_file_declares_hosts_logs_and_jobs(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        scope_file = tmp / "harness-scope.md"
        scope_file.write_text(
            "- workspace_id: ws_x\n- probe_hosts: WWW.twse.com.tw, openapi.twse.com.tw\n"
            "- log_files: logs/a.log, logs/b.log\n"
            "- job: daily = ./venv/bin/python daily_watchlist.py --force\n- job: other = echo x\n"
        )
        scope = ProjectScope.from_file(scope_file)
        self.assertEqual(scope.probe_hosts, ["www.twse.com.tw", "openapi.twse.com.tw"])
        self.assertEqual(scope.log_files, ["logs/a.log", "logs/b.log"])
        self.assertEqual(scope.jobs, {"daily": "./venv/bin/python daily_watchlist.py --force", "other": "echo x"})
        spec = ops_server_spec(scope, tmp / "t.db")
        self.assertEqual(json.loads(spec["env"]["HARNESS_OPS_JOBS"])["other"], "echo x")

    def test_a_scope_with_nothing_declared_has_no_ops_server(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        (tmp / "harness-scope.md").write_text("- workspace_id: ws_x\n")
        self.assertIsNone(ops_server_spec(ProjectScope.from_file(tmp / "harness-scope.md"), None))


if __name__ == "__main__":
    unittest.main()
