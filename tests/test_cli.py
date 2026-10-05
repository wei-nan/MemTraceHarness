import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.cli import (
    build_parser,
    create_dedicated_role_profiles_file,
    update_chat_model_for_project,
    update_digest_fallbacks_for_project,
    update_digest_model_for_project,
    update_recall_fallbacks_for_project,
    update_recall_model_for_project,
    update_role_profile_for_project,
)
from memtrace_harness.config import (
    HarnessConfig,
    project_chat_model_env_var,
    project_chat_provider_env_var,
    project_digest_fallbacks_env_var,
    project_digest_model_env_var,
    project_digest_provider_env_var,
    project_role_profiles_env_var,
)


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

    def test_serve_gateway_loop_skips_due_schedule_past_its_window_end(self) -> None:
        import os
        import signal as signal_module
        from datetime import datetime, timezone
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

        # A weekdays 09:00-13:25 schedule that is somehow still "due" well after
        # its window closed (e.g. the gateway was down over lunch) must not start
        # a new run — it should just roll next_run_at forward and skip.
        due_row = {
            "id": "sched_window1",
            "project": "Beri",
            "goal": "morning backlog review",
            "kind": "weekdays",
            "interval_seconds": None,
            "time_of_day": "09:00",
            "end_time_of_day": "13:25",
            "chat_id": 12345,
        }
        trace_store = MagicMock()

        def fake_list_due(now):
            os.kill(os.getpid(), signal_module.SIGTERM)
            return [due_row]

        trace_store.list_due_schedules.side_effect = fake_list_due

        with patch("memtrace_harness.status_server.start_status_server", return_value=(None, None)):
            with patch("memtrace_harness.cli.datetime") as mock_datetime:
                # 2026-09-22 (Tuesday) 14:00 Asia/Taipei == 06:00 UTC — past the 13:25 window end.
                mock_datetime.now.return_value = datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc)
                mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
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

        gw.run_due_schedule.assert_not_called()
        trace_store.mark_schedule_ran.assert_called_once()
        _args, kwargs = trace_store.mark_schedule_ran.call_args
        self.assertEqual(kwargs["status"], "skipped_window")


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


class UpdateChatModelForProjectTests(TestCase):
    """update_chat_model_for_project() writes a bare relative ".env" — every test
    here runs inside an isolated temp cwd so it can never touch this repo's real
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

    def _build_config(self, tmp_path: Path) -> HarnessConfig:
        scope_dir = tmp_path / "TestProj"
        scope_dir.mkdir()
        scope_path = scope_dir / "harness-scope.md"
        scope_path.write_text(
            "# Harness scope — TestProj\n\n- workspace_id: ws_test\n", encoding="utf-8"
        )
        index_path = tmp_path / "projects.index.txt"
        index_path.write_text(str(scope_path) + "\n", encoding="utf-8")

        return HarnessConfig(
            memtrace_mcp_url=None, memtrace_api_token=None,
            trace_db_path=tmp_path / "trace.sqlite3", trace_root=tmp_path,
            claude_command="claude", codex_command="codex", antigravity_command="agy",
            antigravity_output_mode="auto", cli_timeout_seconds=900,
            telegram_bot_token=None, telegram_allowed_chat_ids=set(),
            project_index_path=index_path, chat_provider="claude", chat_model="haiku",
            unattended_write_requires_approval=True,
        )

    def test_writes_a_project_specific_override_even_without_one_yet(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)

        update_chat_model_for_project(config, "TestProj", "antigravity", "gemini-3.8-flash-high")

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        self.assertIn(f"{project_chat_provider_env_var('TestProj')}=antigravity", env_text)
        self.assertIn(
            f"{project_chat_model_env_var('TestProj')}=gemini-3.8-flash-high", env_text
        )

    def test_digest_model_writes_its_own_vars_and_leaves_chat_alone(self) -> None:
        import os

        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        for var in (project_digest_provider_env_var("TestProj"), project_digest_model_env_var("TestProj")):
            self.addCleanup(os.environ.pop, var, None)

        update_digest_model_for_project(config, "TestProj", "claude", "sonnet")

        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        self.assertIn(f"{project_digest_provider_env_var('TestProj')}=claude", env_text)
        self.assertIn(f"{project_digest_model_env_var('TestProj')}=sonnet", env_text)
        self.assertNotIn("HARNESS_CHAT_", env_text)
        self.assertEqual(config.digest_candidates_for("TestProj"), (("claude", "sonnet"),))

    def test_digest_fallbacks_roundtrip_and_clear(self) -> None:
        import os

        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        var = project_digest_fallbacks_env_var("TestProj")
        for name in (var, project_digest_provider_env_var("TestProj"), project_digest_model_env_var("TestProj")):
            self.addCleanup(os.environ.pop, name, None)
        update_digest_model_for_project(config, "TestProj", "claude", "sonnet")

        update_digest_fallbacks_for_project(config, "TestProj", "codex/gpt-5.6-sol, claude/opus")
        self.assertEqual(
            config.digest_candidates_for("TestProj"),
            (("claude", "sonnet"), ("codex", "gpt-5.6-sol"), ("claude", "opus")),
        )
        update_digest_fallbacks_for_project(config, "TestProj", "")
        self.assertEqual(config.digest_candidates_for("TestProj"), (("claude", "sonnet"),))
        with self.assertRaises(ValueError):
            update_digest_fallbacks_for_project(config, "TestProj", "openai/gpt-5")
        with self.assertRaises(ValueError):
            update_digest_fallbacks_for_project(config, "TestProj", "codex")

    def test_recall_model_and_fallbacks_write_their_own_vars(self) -> None:
        import os

        from memtrace_harness.config import (
            project_recall_fallbacks_env_var,
            project_recall_model_env_var,
            project_recall_provider_env_var,
        )

        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        for name in (
            project_recall_provider_env_var("TestProj"),
            project_recall_model_env_var("TestProj"),
            project_recall_fallbacks_env_var("TestProj"),
        ):
            self.addCleanup(os.environ.pop, name, None)

        update_recall_model_for_project(config, "TestProj", "claude", "claude-sonnet-5-5")
        update_recall_fallbacks_for_project(config, "TestProj", "codex/gpt-5.6-sol")

        self.assertEqual(
            config.recall_candidates_for("TestProj"),
            (("claude", "claude-sonnet-5-5"), ("codex", "gpt-5.6-sol")),
        )
        env_text = (tmp_path / ".env").read_text(encoding="utf-8")
        self.assertNotIn("HARNESS_DIGEST_", env_text)
        with self.assertRaises(ValueError):
            update_recall_fallbacks_for_project(config, "TestProj", "openai/gpt-5")
        with self.assertRaises(ValueError):
            update_recall_model_for_project(config, "TestProj", "openai", "x")

    def test_rejects_unknown_provider(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        with self.assertRaises(ValueError):
            update_chat_model_for_project(config, "TestProj", "openai", "gpt-5")

    def test_rejects_empty_model(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        with self.assertRaises(ValueError):
            update_chat_model_for_project(config, "TestProj", "claude", "   ")

    def test_rejects_unknown_project(self) -> None:
        tmp_path = self._chdir_to_temp()
        config = self._build_config(tmp_path)
        with self.assertRaises(ValueError):
            update_chat_model_for_project(config, "NoSuchProject", "claude", "sonnet")


class CollectStatusDataKnownModelsTests(TestCase):
    def test_known_models_collects_every_configured_pair(self) -> None:
        from memtrace_harness.cli import _collect_status_data

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope_dir = tmp_path / "P"
            scope_dir.mkdir()
            (scope_dir / "harness-scope.md").write_text(
                "# scope\n\n- workspace_id: ws_x\n", encoding="utf-8"
            )
            idx = tmp_path / "projects.index.txt"
            idx.write_text(str(scope_dir / "harness-scope.md") + "\n", encoding="utf-8")

            # Reuse the real packaged default-role-profiles.toml as the fixture
            # instead of hand-writing a minimal one — RoleProfile.from_mapping()
            # requires all five roles plus a fair amount of fallback-related fields
            # to validate at all, and the packaged file is already a known-good
            # example of exactly that shape.
            default_toml = (
                Path(__file__).parent.parent / "src/memtrace_harness/default-role-profiles.toml"
            )
            profiles_path = tmp_path / "profiles.toml"
            profiles_path.write_text(default_toml.read_text(encoding="utf-8"), encoding="utf-8")
            env_var = project_role_profiles_env_var("P")
            os.environ[env_var] = str(profiles_path)
            self.addCleanup(os.environ.pop, env_var, None)

            config = HarnessConfig(
                memtrace_mcp_url=None, memtrace_api_token=None,
                trace_db_path=tmp_path / "trace.sqlite3", trace_root=tmp_path,
                claude_command="claude", codex_command="codex", antigravity_command="agy",
                antigravity_output_mode="auto", cli_timeout_seconds=900,
                telegram_bot_token=None, telegram_allowed_chat_ids=set(),
                project_index_path=idx, chat_provider="antigravity",
                chat_model="gemini-3.6-flash-high", unattended_write_requires_approval=True,
            )
            # No real CLI in unit tests: the provider catalog (agy models, Codex's
            # cache) is stubbed out, so this checks the configured-models half alone.
            from unittest.mock import patch

            with patch("memtrace_harness.model_catalog.provider_models", return_value={}):
                data = _collect_status_data(config)

            self.assertIn("gpt-5.6-luna", data["known_models"]["codex"])
            self.assertIn("sonnet", data["known_models"]["claude"])
            self.assertIn("gemini-3.6-flash-high", data["known_models"]["antigravity"])
            self.assertEqual(set(data["known_models"].keys()), {"claude", "codex", "antigravity"})


class NightlyPreferenceNotificationTests(TestCase):
    def test_the_operator_is_told_what_the_harness_adopted_and_retired(self) -> None:
        from unittest.mock import MagicMock, patch

        from memtrace_harness.cli import _run_nightly_digest_pass
        from memtrace_harness.memory_digest import PreferenceChanges

        changes = PreferenceChanges(
            adopted=[{"id": 7, "text": "回覆一律用繁體中文"}],
            retired=[{"id": 3, "text": "回覆簡短", "retire_reason": "2026-09-30 被 #7 取代"}],
            waiting=[{"id": 8, "text": "多的一條"}],
        )
        gateway = MagicMock()
        config = MagicMock(status_server_host="127.0.0.1", status_server_port=8787)
        with patch("memtrace_harness.cli.run_memory_digests", return_value={"proj": changes}):
            _run_nightly_digest_pass(
                config, MagicMock(), None, [], gateway_for_project={"proj": gateway}, status_bus=None
            )

        message = gateway.notify_all_allowlisted.call_args.args[0]
        self.assertIn("- [#7] 回覆一律用繁體中文", message)
        self.assertIn("撤回 [#3] 回覆簡短（2026-09-30 被 #7 取代）", message)
        self.assertIn("另有 1 條超過每日自動採用上限", message)
        self.assertIn("直接在聊天裡說", message)

    def test_a_night_with_no_preference_changes_sends_nothing(self) -> None:
        from unittest.mock import MagicMock, patch

        from memtrace_harness.cli import _run_nightly_digest_pass
        from memtrace_harness.memory_digest import PreferenceChanges

        gateway = MagicMock()
        with patch("memtrace_harness.cli.run_memory_digests", return_value={"proj": PreferenceChanges()}):
            _run_nightly_digest_pass(
                MagicMock(), MagicMock(), None, [], gateway_for_project={"proj": gateway}, status_bus=None
            )
        gateway.notify_all_allowlisted.assert_not_called()


class ChatCommandTests(TestCase):
    """Codex has no --print (it exits 2 with a usage error), so choosing Codex as a
    chat model used to fail on every message and silently fall back (2026-10-02)."""

    def _config(self):
        from unittest.mock import MagicMock

        from memtrace_harness.config import HarnessConfig

        config = MagicMock(spec=HarnessConfig)
        config.command_for.side_effect = lambda p: f"/bin/{p}"
        return config

    def test_codex_answers_through_exec_read_only(self) -> None:
        from memtrace_harness.config import HarnessConfig

        cmd = HarnessConfig.chat_command(self._config(), "codex", "gpt-6-luna", "你好")
        self.assertEqual(
            cmd,
            ["/bin/codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
             "--color", "never", "--model", "gpt-6-luna", "你好"],
        )
        self.assertNotIn("--print", cmd)

    def test_claude_and_antigravity_keep_print_and_only_claude_gets_the_tool_allowlist(self) -> None:
        from memtrace_harness.config import HarnessConfig

        config = self._config()
        self.assertEqual(
            HarnessConfig.chat_command(config, "claude", "haiku", "p", claude_allowed_tools="mcp__x"),
            ["/bin/claude", "--allowedTools", "mcp__x", "--model", "haiku", "--print", "p"],
        )
        self.assertEqual(
            HarnessConfig.chat_command(config, "antigravity", None, "p", claude_allowed_tools="mcp__x"),
            ["/bin/antigravity", "--print", "p"],
        )

    def test_taiwantrade_proxy_is_attached_to_the_chat_reply_only_when_opted_in(self) -> None:
        from unittest.mock import patch
        from memtrace_harness.config import HarnessConfig

        config = self._config()
        env = {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/keys/tw"}
        with patch.dict("os.environ", env):
            plain = HarnessConfig.chat_command(config, "codex", "m", "p")
            codex = HarnessConfig.chat_command(config, "codex", "m", "p", taiwantrade=True)
            claude = HarnessConfig.chat_command(
                config, "claude", "haiku", "p", claude_allowed_tools="mcp__x", taiwantrade=True
            )
        # Not requested -> never attached; digests/classifiers don't get trading data.
        self.assertNotIn("mcp_servers.taiwantrade.command=", " ".join(plain))
        self.assertIn("read-only", codex)  # the sandbox stays read-only
        self.assertTrue(any(a.startswith("mcp_servers.taiwantrade.command=") for a in codex))
        self.assertTrue(any("/keys/tw" in a for a in codex))  # a path, never the key itself
        # exec mode has approval=never: without this the MCP call is rejected outright.
        self.assertIn('mcp_servers.taiwantrade.default_tools_approval_mode="approve"', codex)
        self.assertEqual(claude[claude.index("--allowedTools") + 1], "mcp__x,mcp__taiwantrade")
        self.assertIn("--mcp-config", claude)
        # No opt-in env var -> nothing attached even when asked for.
        with patch.dict("os.environ", {}, clear=True):
            off = HarnessConfig.chat_command(config, "codex", "m", "p", taiwantrade=True)
        self.assertNotIn("mcp_servers.taiwantrade.command=", " ".join(off))


class BotPollerTests(TestCase):
    """2026-10-02: four bots were polled one after another (each long-poll blocking up
    to 30s) and messages were answered in the same loop, so a message could wait ~90s
    before it was even read, and one bot's slow reply held up every other bot."""

    def _gateway(self, name: str, poll):
        from unittest.mock import MagicMock

        gw = MagicMock()
        project = MagicMock()
        project.name = name
        gw.projects = [project]
        gw.poll_once.side_effect = poll
        return gw

    def test_a_busy_bot_does_not_hold_up_the_others(self) -> None:
        import threading

        from memtrace_harness.cli import start_bot_pollers

        stop = threading.Event()
        release_slow = threading.Event()
        fast_handled = threading.Event()

        def slow_poll(timeout):
            release_slow.wait(5)  # e.g. a 2-minute chat reply in progress
            return 0

        def fast_poll(timeout):
            fast_handled.set()
            stop.wait(0.05)
            return 1

        slow, fast = self._gateway("Slow", slow_poll), self._gateway("Fast", fast_poll)
        threads = start_bot_pollers([slow, fast], stop, poll_timeout=30)
        try:
            self.assertTrue(fast_handled.wait(2), "the fast bot was blocked behind the slow one")
            self.assertFalse(release_slow.is_set())
        finally:
            stop.set()
            release_slow.set()
            for t in threads:
                t.join(5)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual({t.name for t in threads}, {"telegram-poll-Slow", "telegram-poll-Fast"})

    def test_a_failing_poll_backs_off_instead_of_spinning(self) -> None:
        import threading
        from unittest.mock import patch

        from memtrace_harness import cli

        stop = threading.Event()
        calls = []

        def broken(timeout):
            calls.append(1)
            raise RuntimeError("network down")

        gw = self._gateway("Broken", broken)
        with patch.object(cli, "POLL_FAILURE_BACKOFF_SECONDS", 0.2):
            threads = cli.start_bot_pollers([gw], stop, poll_timeout=30)
            stop.wait(0.5)
            stop.set()
            for t in threads:
                t.join(2)
        self.assertLessEqual(len(calls), 4)
        self.assertGreaterEqual(len(calls), 1)
