import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.cli import build_parser, load_scope, main
from memtrace_harness.config import HarnessConfig


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

    def test_serve_takes_the_workspace_as_a_global_option(self) -> None:
        args = build_parser().parse_args(["-C", "/some/workspace", "serve", "--scan-interval-seconds", "600"])
        self.assertEqual((args.command, args.workspace_dir.as_posix(), args.scan_interval_seconds), ("serve", "/some/workspace", 600))

    def test_init_workspace_accepts_an_empty_folder_and_flags(self) -> None:
        args = build_parser().parse_args(
            ["init-workspace", "/tmp/new-ws", "--memtrace-workspace", "ws_abc", "--non-interactive"]
        )
        self.assertEqual((args.command, args.directory.as_posix(), args.memtrace_workspace), ("init-workspace", "/tmp/new-ws", "ws_abc"))

    def test_the_gateway_and_project_index_commands_are_gone(self) -> None:
        for removed in ("gateway", "init-project", "remove-project"):
            with self.subTest(command=removed), self.assertRaises(SystemExit):
                build_parser().parse_args([removed])

    def test_load_scope_needs_a_workspace_with_a_scope_file(self) -> None:
        with TemporaryDirectory() as tmp:
            config = MagicMock(workspace_root=None)
            with self.assertRaises(ValueError):
                load_scope(config)
            config = MagicMock(workspace_root=Path(tmp))
            with self.assertRaises(ValueError):
                load_scope(config)
            (Path(tmp) / "harness-scope.md").write_text("- workspace_id: ws_x\n", encoding="utf-8")
            scope = load_scope(config)
            self.assertEqual((scope.workspace_id, scope.name), ("ws_x", Path(tmp).name))
            # the Harness's own files are never the agents' to read
            self.assertIn(".harness.env", scope.off_limits)
            self.assertIn(".harness", scope.off_limits)

    def test_main_loads_the_workspace_env_before_running_a_command(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / ".harness.env").write_text("HARNESS_CHAT_MODEL=from-the-file\n", encoding="utf-8")
            seen = {}

            def fake_status(args):
                seen["model"] = os.environ.get("HARNESS_CHAT_MODEL")
                seen["workspace"] = os.environ.get("HARNESS_WORKSPACE")
                return 0

            with patch.dict(os.environ, {}, clear=False), patch("memtrace_harness.cli.status_command", fake_status):
                os.environ.pop("HARNESS_CHAT_MODEL", None)
                self.assertEqual(main(["-C", tmp, "status"]), 0)
                os.environ.pop("HARNESS_CHAT_MODEL", None)
                os.environ.pop("HARNESS_WORKSPACE", None)
        self.assertEqual(seen["model"], "from-the-file")
        self.assertEqual(seen["workspace"], str(Path(tmp).resolve()))


class ChatCommandTests(TestCase):
    """Codex has no --print (it exits 2 with a usage error), so choosing Codex as a
    chat model used to fail on every message and silently fall back (2026-10-02)."""

    def _config(self):
        from unittest.mock import MagicMock

        from memtrace_harness.config import HarnessConfig

        config = MagicMock(spec=HarnessConfig)
        config.command_for.side_effect = lambda p: f"/bin/{p}"
        config.source_command.side_effect = lambda p: [config.command_for(p)]
        config.trace_db_path = Path("/tmp/harness-test.sqlite3")
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
            ["/bin/claude", "--allowedTools", "mcp__x,Skill", "--model", "haiku", "--print", "p"],
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
        self.assertEqual(claude[claude.index("--allowedTools") + 1], "mcp__x,mcp__taiwantrade,Skill")
        self.assertIn("--mcp-config", claude)
        # No opt-in env var -> nothing attached even when asked for.
        with patch.dict("os.environ", {}, clear=True):
            off = HarnessConfig.chat_command(config, "codex", "m", "p", taiwantrade=True)
        self.assertNotIn("mcp_servers.taiwantrade.command=", " ".join(off))

    def test_codex_chat_gets_read_only_memtrace_only_when_asked_and_configured(self) -> None:
        from dataclasses import replace
        from unittest.mock import patch
        from memtrace_harness.config import HarnessConfig

        config = self._config()
        try:
            config = replace(config, memtrace_mcp_url="https://mt.example/mcp")
        except TypeError:
            config.memtrace_mcp_url = "https://mt.example/mcp"
        with patch.dict("os.environ", {"MEMTRACE_API_TOKEN": "secret-value"}):
            on = HarnessConfig.chat_command(config, "codex", "m", "p", memtrace_read=True)
            off = HarnessConfig.chat_command(config, "codex", "m", "p")
        joined = " ".join(on)
        self.assertIn('mcp_servers.memtrace.url="https://mt.example/mcp"', joined)
        self.assertIn('enabled_tools=["search_nodes", "get_node", "list_nodes", "traverse"]', joined)
        self.assertIn('bearer_token_env_var="MEMTRACE_API_TOKEN"', joined)
        self.assertNotIn("secret-value", joined)  # the token goes by env var name only
        self.assertNotIn("mcp_servers.memtrace", " ".join(off))

    def test_chat_notes_become_validated_note_ops(self) -> None:
        from memtrace_harness.completion_claims import chat_notes_to_ops

        ops = chat_notes_to_ops(
            ["factual::GCP 分客戶::每客戶一個 GCP Project:: 由 Org 管理", "bad", "nonsense::t::b", "factual::::empty title"]
        )
        self.assertEqual([o["title"] for o in ops], ["GCP 分客戶", "t"])
        self.assertEqual(ops[0]["body"], "每客戶一個 GCP Project:: 由 Org 管理")
        self.assertEqual(ops[1]["content_type"], "factual")  # unknown type falls back, not dropped
