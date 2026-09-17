from unittest import TestCase

from memtrace_harness.cli import build_parser


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

        with patch("memtrace_harness.status_server.start_status_server", return_value=(None, None)):
            _serve_gateway_loop(
                [gw],
                scanner,
                MagicMock(),
                [],
                config,
                poll_timeout=1,
                scan_interval=9999,
                consolidation_interval=9999,
            )

        gw.notify_all_allowlisted.assert_called_once()
        (notified_text,), _ = gw.notify_all_allowlisted.call_args
        self.assertIn("已啟動", notified_text)
        self.assertIn("Beri", notified_text)
