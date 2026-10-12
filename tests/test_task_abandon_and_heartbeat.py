from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness import cli_process
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scope import ProjectScope
from memtrace_harness.service import HarnessService
from memtrace_harness.trace_store import TraceStore


def _service(tmp: Path) -> HarnessService:
    db = tmp / "t.sqlite3"
    store = TraceStore(db)
    config = HarnessConfig(
        memtrace_mcp_url=None, memtrace_api_token=None, trace_db_path=db, trace_root=tmp,
        claude_command="claude", codex_command="codex", antigravity_command="agy", antigravity_output_mode="auto",
        cli_timeout_seconds=900, chat_provider="claude", chat_model="haiku", unattended_write_requires_approval=True,
    )
    scope = ProjectScope(name="P", workspace_id="ws_t", working_directory=tmp, scope_file_path=tmp / "harness-scope.md")
    return HarnessService(config, ApprovalManager(store), ChatTriage([scope]), PrimarySessionManager(store), [scope])


def _spawn(argv: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(argv, cwd=cwd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class KillProviderClisTests(TestCase):
    def test_only_a_provider_cli_group_inside_the_directory_is_stopped(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        elsewhere = Path(tempfile.mkdtemp()).resolve()
        fake_codex = tmp / "codex"
        fake_codex.write_text("#!/bin/sh\nsleep 60\n")
        fake_codex.chmod(0o755)
        cli = _spawn([str(fake_codex)], tmp)
        shell = _spawn(["sleep", "60"], tmp)  # a person's own process in the folder: not a provider CLI
        other_cli = _spawn([str(fake_codex)], elsewhere)  # a provider CLI in another folder
        try:
            time.sleep(0.5)
            stopped = cli_process.kill_provider_clis_in_directory(tmp)
            cli.wait(timeout=10)
            self.assertIn(cli.pid, stopped)
            self.assertIsNone(shell.poll())
            self.assertIsNone(other_cli.poll())
        finally:
            for proc in (cli, shell, other_cli):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()


class AbandonTaskTests(TestCase):
    def test_abandoning_stops_the_worktree_cli_then_marks_failed(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        service = _service(tmp)
        store = service.approval_manager.trace_store
        with patch.object(store, "get_conversation_worktree", return_value={"path": str(tmp / "wt")}), patch.object(
            store, "abandon_conversation", return_value=True
        ) as abandon, patch("memtrace_harness.service.kill_provider_clis_in_directory", return_value=[11, 12]) as kill:
            self.assertEqual(service.abandon_task("ws_t", "chat_x"), (True, 2))
        kill.assert_called_once_with(str(tmp / "wt"))
        abandon.assert_called_once_with("ws_t", "chat_x")

    def test_a_task_without_a_worktree_is_never_swept_by_directory(self) -> None:
        tmp = Path(tempfile.mkdtemp()).resolve()
        service = _service(tmp)
        store = service.approval_manager.trace_store
        with patch.object(store, "get_conversation_worktree", return_value=None), patch.object(
            store, "abandon_conversation", return_value=True
        ), patch("memtrace_harness.service.kill_provider_clis_in_directory") as kill:
            self.assertEqual(service.abandon_task("ws_t", "chat_x"), (True, 0))
        kill.assert_not_called()


class LongRunningNoteTests(TestCase):
    def _entry(self, started_at: float) -> dict:
        return {"conversation_id": "chat_x", "workspace_id": "ws_t", "started_at": started_at}

    def test_notes_after_ten_quiet_minutes_then_at_most_every_twenty(self) -> None:
        service = _service(Path(tempfile.mkdtemp()))
        sent: list[str] = []
        service.notify_operator = sent.append  # type: ignore[method-assign]
        entry = self._entry(1000.0)
        with patch("memtrace_harness.service.default_tracker.snapshot", return_value=[entry]):
            self.assertEqual(service.report_long_running(now=1000.0 + 599), 0)
            self.assertEqual(service.report_long_running(now=1000.0 + 600), 1)
            self.assertEqual(service.report_long_running(now=1000.0 + 600 + 1199), 0)
            self.assertEqual(service.report_long_running(now=1000.0 + 600 + 1200), 1)
        self.assertEqual(len(sent), 2)
        self.assertIn("chat_x", sent[0])
        self.assertIn("仍在執行", sent[0])

    def test_a_schedule_run_is_never_noted_and_finished_tasks_are_forgotten(self) -> None:
        service = _service(Path(tempfile.mkdtemp()))
        sent: list[str] = []
        service.notify_operator = sent.append  # type: ignore[method-assign]
        store = service.approval_manager.trace_store
        entry = self._entry(0.0)
        with patch("memtrace_harness.service.default_tracker.snapshot", return_value=[entry]), patch.object(
            store, "list_workspace_locks", return_value=[{"conversation_id": "chat_x", "schedule_id": "sched_1"}]
        ):
            self.assertEqual(service.report_long_running(now=5000.0), 0)
        with patch("memtrace_harness.service.default_tracker.snapshot", return_value=[entry]):
            service.report_long_running(now=5000.0)
        self.assertIn("chat_x", service._long_run_notes)
        with patch("memtrace_harness.service.default_tracker.snapshot", return_value=[]):
            service.report_long_running(now=6000.0)
        self.assertNotIn("chat_x", service._long_run_notes)
