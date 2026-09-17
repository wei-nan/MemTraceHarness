from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.cli import (
    build_parser,
    create_dedicated_role_profiles_file,
    update_role_profile_for_project,
)
from memtrace_harness.config import HarnessConfig, project_role_profiles_env_var


class CliTests(TestCase):
    def test_run_requires_exactly_one_agent(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--workspace",
                "ws_spec_plan",
                "--goal",
                "Inspect the repository",
                "--agent",
                "codex",
            ]
        )

        self.assertEqual(args.agent, "codex")

    def test_run_rejects_repeated_agent_flag(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "run",
                    "--workspace",
                    "ws_spec_plan",
                    "--goal",
                    "Inspect the repository",
                    "--agent",
                    "claude",
                    "--agent",
                    "codex",
                ]
            )

    def test_loop_accepts_profiles_and_hard_budget_without_agent_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "loop",
                "--workspace",
                "ws_spec_plan",
                "--goal",
                "Implement the accepted change",
                "--risk-level",
                "high",
                "--profiles-file",
                "profiles.toml",
                "--max-total-tokens",
                "120000",
                "--conversation-id",
                "conv_existing",
            ]
        )

        self.assertEqual(args.command, "loop")
        self.assertEqual(args.risk_level, "high")
        self.assertEqual(args.max_total_tokens, 120000)
        self.assertEqual(args.conversation_id, "conv_existing")

    def test_probe_command_execution(self) -> None:
        from unittest.mock import patch, MagicMock
        from memtrace_harness.cli import probe_command
        from memtrace_harness.cli_process import ProcessResult

        fake_res = ProcessResult(
            command=["claude", "--version"],
            return_code=0,
            stdout="claude 2.1.0",
            stderr="",
            started_at="2026-08-05T00:00:00Z",
            completed_at="2026-08-05T00:00:00Z",
            duration_ms=10,
        )
        parser = build_parser()
        args = parser.parse_args(["probe", "--agent", "claude"])
        with patch("memtrace_harness.cli.CliProcessRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.probe.return_value = fake_res
            mock_runner.run.return_value = fake_res
            mock_runner_cls.return_value = mock_runner
            exit_code = probe_command(args)
            self.assertEqual(exit_code, 0)

    def test_probe_command_json_output(self) -> None:
        from unittest.mock import patch, MagicMock
        from memtrace_harness.cli import probe_command
        from memtrace_harness.cli_process import ProcessResult

        fake_res = ProcessResult(
            command=["codex", "--version"],
            return_code=0,
            stdout="codex 0.14.0",
            stderr="",
            started_at="2026-08-05T00:00:00Z",
            completed_at="2026-08-05T00:00:00Z",
            duration_ms=10,
        )
        parser = build_parser()
        args = parser.parse_args(["probe", "--json"])
        with patch("memtrace_harness.cli.CliProcessRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.probe.return_value = fake_res
            mock_runner.run.return_value = fake_res
            mock_runner_cls.return_value = mock_runner
            exit_code = probe_command(args)
            self.assertEqual(exit_code, 0)

    def test_serve_gateway_loop_notifies_each_bot_on_startup(self) -> None:
        import os
        import signal as signal_module
        from unittest.mock import MagicMock, patch

        from memtrace_harness.cli import _serve_gateway_loop

        scope = MagicMock()
        scope.name = "Beri"
        gw = MagicMock()
        gw.projects = [scope]

        def fake_poll_once(timeout: int = 30) -> int:
            # First loop iteration signals the process to stop, so the loop exits
            # right after the startup notification without blocking on a real
            # long-poll or waiting for an actual OS signal delivery race.
            os.kill(os.getpid(), signal_module.SIGTERM)
            return 0

        gw.poll_once.side_effect = fake_poll_once

        scanner = MagicMock()
        config = MagicMock()
        config.shutdown_grace_seconds = 0
        config.schedule_timezone = "Asia/Taipei"
        trace_store = MagicMock()
        trace_store.list_due_schedules.return_value = []

        with patch("memtrace_harness.status_server.start_status_server", return_value=(None, None)):
            _serve_gateway_loop(
                [gw],
                scanner,
                MagicMock(),
                [],
                config,
                trace_store,
                {"Beri": gw},
                poll_timeout=1,
                scan_interval=9999,
                consolidation_interval=9999,
                schedule_check_interval=9999,
            )

        gw.notify_all_allowlisted.assert_called_once()
        (notified_text,), _ = gw.notify_all_allowlisted.call_args
        self.assertIn("已啟動", notified_text)
        self.assertIn("Beri", notified_text)

    def test_serve_gateway_loop_triggers_due_schedules(self) -> None:
        import os
        import signal as signal_module
        from unittest.mock import MagicMock, patch

        from memtrace_harness.cli import _serve_gateway_loop

        scope = MagicMock()
        scope.name = "Beri"
        gw = MagicMock()
        gw.projects = [scope]
        gw.poll_once.return_value = 0

        scanner = MagicMock()
        config = MagicMock()
        config.shutdown_grace_seconds = 0
        config.schedule_timezone = "Asia/Taipei"

        due_row = {
            "id": "sched_abc123",
            "project": "Beri",
            "goal": "daily backlog review",
            "kind": "interval",
            "interval_seconds": 3600,
            "time_of_day": None,
            "chat_id": 12345,
        }
        trace_store = MagicMock()

        def fake_list_due(now):
            # Stop the loop right after the first schedule check so this test
            # doesn't block on a real poll cycle or an OS signal race.
            os.kill(os.getpid(), signal_module.SIGTERM)
            return [due_row]

        trace_store.list_due_schedules.side_effect = fake_list_due

        with patch("memtrace_harness.status_server.start_status_server", return_value=(None, None)):
            _serve_gateway_loop(
                [gw],
                scanner,
                MagicMock(),
                [],
                config,
                trace_store,
                {"Beri": gw},
                poll_timeout=1,
                scan_interval=9999,
                consolidation_interval=9999,
                schedule_check_interval=1,
            )

        gw.run_due_schedule.assert_called_once_with(due_row)
        trace_store.mark_schedule_ran.assert_called_once()
        _args, kwargs = trace_store.mark_schedule_ran.call_args
        self.assertEqual(kwargs["status"], "triggered")


class UpdateRoleProfileForProjectTests(TestCase):
    """Covers the status dashboard's one write endpoint (POST /api/role-profile) at
    the level that actually matters: does the TOML file end up correct and are the
    guardrails (unknown provider, shared-default project, unknown profile) real."""

    def _build_project(self, tmp_path: Path, *, dedicated_profiles: bool):
        import os

        scope_dir = tmp_path / "TestProj"
        scope_dir.mkdir()
        scope_path = scope_dir / "harness-scope.md"
        scope_path.write_text(
            "# Harness scope — TestProj\n\n- workspace_id: ws_test\n", encoding="utf-8"
        )
        index_path = tmp_path / "projects.index.txt"
        index_path.write_text(str(scope_path) + "\n", encoding="utf-8")

        profiles_path = tmp_path / "profiles.toml"
        profiles_path.write_text(
            '[profiles.controller]\n'
            'role = "Controller"\n'
            'provider = "codex"\n'
            'model = "gpt-5.6-luna"\n'
            'reasoning_effort = "medium"\n'
            '\n'
            '# A decision-rationale comment that must survive the edit untouched.\n'
            '[[profiles.controller.fallbacks]]\n'
            'provider = "claude"\n'
            'model = "sonnet"\n'
            '\n'
            '[profiles.planner]\n'
            'role = "Planner"\n'
            'provider = "claude"\n'
            'model = "sonnet"\n',
            encoding="utf-8",
        )
        env_var = "HARNESS_ROLE_PROFILES_FILE_TESTPROJ"
        if dedicated_profiles:
            os.environ[env_var] = str(profiles_path)
        else:
            os.environ.pop(env_var, None)
        self.addCleanup(os.environ.pop, env_var, None)

        config = HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None,
            trace_db_path=tmp_path / "trace.sqlite3", trace_root=tmp_path,
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900,
            telegram_bot_token=None, telegram_allowed_chat_ids=set(),
            project_index_path=index_path, chat_provider="claude", chat_model="haiku",
            unattended_write_requires_approval=True,
        )
        return config, profiles_path

    def test_updates_provider_and_model_preserving_comments_and_fallback(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, profiles_path = self._build_project(tmp_path, dedicated_profiles=True)

            update_role_profile_for_project(config, "TestProj", "controller", "claude", "opus")

            text = profiles_path.read_text(encoding="utf-8")
            self.assertIn('provider = "claude"', text.splitlines()[2])
            self.assertIn('model = "opus"', text.splitlines()[3])
            self.assertIn("decision-rationale comment that must survive", text)
            # The fallback's own provider/model must be untouched.
            self.assertIn('[[profiles.controller.fallbacks]]\nprovider = "claude"\nmodel = "sonnet"', text)
            # A different profile in the same file is untouched.
            self.assertIn('[profiles.planner]\nrole = "Planner"\nprovider = "claude"\nmodel = "sonnet"', text)

    def test_rejects_unknown_provider(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, _profiles_path = self._build_project(tmp_path, dedicated_profiles=True)
            with self.assertRaises(ValueError):
                update_role_profile_for_project(config, "TestProj", "controller", "openai", "gpt-5")

    def test_rejects_model_with_a_quote(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, _profiles_path = self._build_project(tmp_path, dedicated_profiles=True)
            with self.assertRaises(ValueError):
                update_role_profile_for_project(config, "TestProj", "controller", "claude", 'sonnet"; x=1')

    def test_refuses_project_still_on_shared_default(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, profiles_path = self._build_project(tmp_path, dedicated_profiles=False)
            with self.assertRaises(ValueError) as ctx:
                update_role_profile_for_project(config, "TestProj", "controller", "claude", "opus")
            self.assertIn("shared default", str(ctx.exception))
            # Nothing written anywhere — the shared file isn't even touched.
            self.assertNotIn('provider = "claude"\nmodel = "opus"', profiles_path.read_text())

    def test_rejects_unknown_project(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, _profiles_path = self._build_project(tmp_path, dedicated_profiles=True)
            with self.assertRaises(ValueError):
                update_role_profile_for_project(config, "NoSuchProject", "controller", "claude", "opus")

    def test_rejects_unknown_profile_id(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config, _profiles_path = self._build_project(tmp_path, dedicated_profiles=True)
            with self.assertRaises(ValueError):
                update_role_profile_for_project(config, "TestProj", "red-team", "claude", "opus")


class CreateDedicatedRoleProfilesFileTests(TestCase):
    """create_dedicated_role_profiles_file() writes to bare relative "profiles/" and
    ".env" paths (same as init-project's own wizard code) — every test here runs
    inside an isolated temp cwd so it can never touch this repo's real profiles/ or
    .env, restored via addCleanup even if the test body raises."""

    def _chdir_to_temp(self) -> Path:
        import os

        tmp_dir_ctx = TemporaryDirectory()
        self.addCleanup(tmp_dir_ctx.cleanup)
        tmp_path = Path(tmp_dir_ctx.name)
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        self.addCleanup(os.chdir, original_cwd)
        return tmp_path

    def _build_config(self, tmp_path: Path):
        import os

        scope_dir = tmp_path / "TestProj"
        scope_dir.mkdir()
        scope_path = scope_dir / "harness-scope.md"
        scope_path.write_text(
            "# Harness scope — TestProj\n\n- workspace_id: ws_test\n", encoding="utf-8"
        )
        index_path = tmp_path / "projects.index.txt"
        index_path.write_text(str(scope_path) + "\n", encoding="utf-8")
        env_var = project_role_profiles_env_var("TestProj")
        os.environ.pop(env_var, None)
        self.addCleanup(os.environ.pop, env_var, None)

        return HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None,
            trace_db_path=tmp_path / "trace.sqlite3", trace_root=tmp_path,
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900,
            telegram_bot_token=None, telegram_allowed_chat_ids=set(),
            project_index_path=index_path, chat_provider="claude", chat_model="haiku",
            unattended_write_requires_approval=True,
        )

    def test_copies_default_profiles_and_writes_env_var(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)

        dest = create_dedicated_role_profiles_file(config, "TestProj")

        self.assertTrue(dest.is_file())
        self.assertIn("[profiles.controller]", dest.read_text(encoding="utf-8"))
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        self.assertIn(f"{project_role_profiles_env_var('TestProj')}={dest}", env_text)

    def test_refuses_when_already_dedicated(self) -> None:
        import os

        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        env_var = project_role_profiles_env_var("TestProj")
        existing = tmp_path / "already-dedicated.toml"
        existing.write_text("[profiles.controller]\n", encoding="utf-8")
        os.environ[env_var] = str(existing)

        with self.assertRaises(ValueError) as ctx:
            create_dedicated_role_profiles_file(config, "TestProj")
        self.assertIn("already has", str(ctx.exception))

    def test_rejects_unknown_project(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        with self.assertRaises(ValueError):
            create_dedicated_role_profiles_file(config, "NoSuchProject")
