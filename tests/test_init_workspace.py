import argparse
import socket
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.cli import build_parser, load_scope
from memtrace_harness.config import HarnessConfig
from memtrace_harness.init_workspace import (
    default_peer_description, free_port, init_workspace_command, write_scope_file, write_telegram_env,
)
from memtrace_harness.workspace import ENV_FILE_NAME, load_workspace, models_file, parse_env_file, read_model_settings, update_model_settings


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


class PeerDescriptionDefaultTests(TestCase):
    """A new Harness is introduced to the others from the start (docs/a2a-peers.md)."""

    def test_a_new_workspace_gets_an_introduction_in_models_toml_not_in_the_secrets_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            init_workspace_command(args(root, name="Gamma", memtrace_workspace="ws_abc"))
            text = read_model_settings(models_file(root))["HARNESS_PEER_DESCRIPTION"]
            self.assertIn("專案「Gamma」的 Harness", text)
            self.assertIn("ask_peer", text)
            self.assertNotIn("HARNESS_PEER_DESCRIPTION", (root / ENV_FILE_NAME).read_text())

    def test_the_one_line_purpose_is_included(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            init_workspace_command(args(root, name="Gamma", memtrace_workspace="ws_abc", description="  做台股回測。\n 每天更新觀察清單 "))
            text = read_model_settings(models_file(root))["HARNESS_PEER_DESCRIPTION"]
            self.assertIn("做台股回測。 每天更新觀察清單。", text)  # one line, one full stop

    def test_a_rerun_never_overwrites_what_the_operator_wrote(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            init_workspace_command(args(root, name="Gamma", memtrace_workspace="ws_abc"))
            update_model_settings(models_file(root), {"HARNESS_PEER_DESCRIPTION": "我自己寫的"})
            init_workspace_command(args(root, name="Gamma", description="另一句"))
            self.assertEqual(read_model_settings(models_file(root))["HARNESS_PEER_DESCRIPTION"], "我自己寫的")

    def test_the_default_fits_the_page_limit_even_with_a_long_purpose(self) -> None:
        from memtrace_harness.admin import PEER_DESCRIPTION_MAX_CHARS

        self.assertLessEqual(len(default_peer_description("N" * 40, "字" * 5000)), PEER_DESCRIPTION_MAX_CHARS)
        self.assertNotIn("None", default_peer_description("x"))
        self.assertTrue(default_peer_description("x").startswith("專案「x」的 Harness。想知道"))


class HarnessNameDefaultTests(TestCase):
    def test_a_new_workspace_is_named_after_its_project_unless_told_otherwise(self) -> None:
        with TemporaryDirectory() as tmp:
            one, two = Path(tmp) / "one", Path(tmp) / "two"
            init_workspace_command(args(one, name="Gamma Project", memtrace_workspace="ws_abc"))
            self.assertEqual(read_model_settings(models_file(one))["HARNESS_PEER_NAME"], "gamma-project")
            init_workspace_command(args(two, name="Gamma", memtrace_workspace="ws_abc", harness_name="gamma-app"))
            self.assertEqual(read_model_settings(models_file(two))["HARNESS_PEER_NAME"], "gamma-app")

    def test_a_bad_name_is_refused_and_a_rerun_keeps_a_rename(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            with self.assertRaises(ValueError):
                init_workspace_command(args(root, name="Gamma", memtrace_workspace="ws_abc", harness_name="Bad Name"))
            init_workspace_command(args(root, name="Gamma", memtrace_workspace="ws_abc"))
            update_model_settings(models_file(root), {"HARNESS_PEER_NAME": "mine"})
            init_workspace_command(args(root, name="Gamma", harness_name="other"))
            self.assertEqual(read_model_settings(models_file(root))["HARNESS_PEER_NAME"], "mine")

    def test_a_project_name_with_no_usable_letters_still_gets_a_working_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            init_workspace_command(args(root, name="專案", memtrace_workspace="ws_abc"))
            self.assertNotIn("HARNESS_PEER_NAME", read_model_settings(models_file(root)))  # the Harness falls back to "harness"
