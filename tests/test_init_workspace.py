import argparse
import socket
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.cli import build_parser, load_scope
from memtrace_harness.config import HarnessConfig
from memtrace_harness.init_workspace import free_port, init_workspace_command, write_scope_file, write_telegram_env
from memtrace_harness.workspace import ENV_FILE_NAME, load_workspace, parse_env_file


def args(directory, **kw):
    ns = argparse.Namespace(directory=Path(directory), name=None, memtrace_workspace=None, non_interactive=True, workspace_dir=None)
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


class InitWorkspaceTests(TestCase):
    def test_an_empty_folder_becomes_a_ready_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            self.assertEqual(init_workspace_command(args(root, memtrace_workspace="ws_abc")), 0)
            env = parse_env_file(root / ENV_FILE_NAME)
            self.assertGreaterEqual(len(env["HARNESS_API_TOKEN"]), 32)
            self.assertTrue(env["HARNESS_API_PORT"].isdigit())
            self.assertEqual(stat.S_IMODE((root / ENV_FILE_NAME).stat().st_mode), 0o600)
            self.assertTrue((root / ".harness").is_dir())
            self.assertFalse((root / ".git").exists())
            # and the Harness can read it back as a project
            import os
            from unittest.mock import patch

            with patch.dict(os.environ, {}):
                for key in list(os.environ):
                    if key.startswith("HARNESS_"):
                        os.environ.pop(key)
                load_workspace(root)
                scope = load_scope(HarnessConfig.from_env())
            self.assertEqual((scope.name, scope.workspace_id), ("fresh", "ws_abc"))
            self.assertTrue(scope.working_directory.samefile(root))
            self.assertIn(".harness.env", scope.off_limits)

    def test_a_scope_file_that_exists_is_kept_and_a_rerun_keeps_the_token(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "harness-scope.md").write_text("- workspace_id: ws_mine\n- off_limits: secrets\n", encoding="utf-8")
            init_workspace_command(args(tmp))
            first = parse_env_file(Path(tmp) / ENV_FILE_NAME)
            init_workspace_command(args(tmp))
            second = parse_env_file(Path(tmp) / ENV_FILE_NAME)
            self.assertEqual(first, second)
            self.assertEqual((Path(tmp) / "harness-scope.md").read_text(encoding="utf-8"), "- workspace_id: ws_mine\n- off_limits: secrets\n")

    def test_without_a_scope_file_a_memtrace_workspace_is_required(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                init_workspace_command(args(Path(tmp) / "x"))

    def test_the_folder_may_come_from_the_global_option(self) -> None:
        with TemporaryDirectory() as tmp:
            parsed = build_parser().parse_args(["-C", tmp, "init-workspace", "--non-interactive", "--memtrace-workspace", "ws_1"])
            self.assertIsNone(parsed.directory)
            self.assertEqual(init_workspace_command(parsed), 0)
            self.assertTrue((Path(tmp) / ENV_FILE_NAME).is_file())

    def test_free_port_skips_busy_and_taken_ports(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            port = busy.getsockname()[1]
            self.assertNotEqual(free_port(port), port)
        self.assertNotEqual(free_port(port, {port, port + 1}), port + 1)

    def test_the_scope_file_guards_the_harness_own_files(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-scope.md"
            write_scope_file(path, "p", "ws_1", "secrets, prod")
            text = path.read_text(encoding="utf-8")
            self.assertIn("- off_limits: .harness.env, .harness, secrets, prod", text)

    def test_the_telegram_env_is_private_and_holds_what_the_service_needs(self) -> None:
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "svc" / "telegram.env"
            write_telegram_env(target, harness_port=8801, harness_token="h", bot_token="b", chat_ids="1,2")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(
                parse_env_file(target),
                {"TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_ALLOWED_CHAT_IDS": "1,2",
                 "HARNESS_URL": "http://127.0.0.1:8801", "HARNESS_API_TOKEN": "h"},
            )
