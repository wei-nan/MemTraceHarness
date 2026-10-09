import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.config import HarnessConfig
from memtrace_harness.workspace import (
    ENV_FILE_NAME,
    WorkspaceError,
    env_file_problems,
    init_workspace,
    load_workspace,
    parse_env_file,
    remove_env_keys,
    write_env_updates,
)
from tests.workspace_support import workspace


class EnvFileTests(TestCase):
    def test_parses_comments_export_and_quotes_without_expanding_anything(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".harness.env"
            path.write_text(
                "# comment\n\nA=1\nexport B = two words \nC=\"quoted\"\nD='single'\nE=$HOME\nnot a line\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_env_file(path), {"A": "1", "B": "two words", "C": "quoted", "D": "single", "E": "$HOME"}
            )

    def test_updates_keep_comments_other_keys_and_restrict_the_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / ".harness.env"
            path.write_text("# keep me\nA=1\nB=2\n", encoding="utf-8")
            write_env_updates(path, {"B": "3", "C": "4"})
            self.assertEqual(path.read_text(encoding="utf-8"), "# keep me\nA=1\nB=3\nC=4\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            remove_env_keys(path, ["A", "C"])
            self.assertEqual(path.read_text(encoding="utf-8"), "# keep me\nB=3\n")


class WorkspaceTests(TestCase):
    def test_an_empty_folder_becomes_a_workspace(self) -> None:
        with TemporaryDirectory() as tmp:
            paths = init_workspace(Path(tmp) / "new")
            self.assertTrue(paths["env"].is_file())
            self.assertTrue((paths["state"] / ".gitignore").is_file())  # state and secrets never get committed
            self.assertEqual(stat.S_IMODE(paths["env"].stat().st_mode), 0o600)
            self.assertFalse((Path(tmp) / "new" / ".git").exists())  # no git needed

    def test_a_git_checkout_keeps_the_secrets_out_of_git_status_without_touching_its_gitignore(self) -> None:
        import subprocess

        with TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (Path(tmp) / ".gitignore").write_text("build/\n", encoding="utf-8")
            paths = init_workspace(Path(tmp))
            init_workspace(Path(tmp))  # idempotent
            exclude = (Path(tmp) / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(exclude.count(".harness.env"), 1)
            status = subprocess.run(
                ["git", "-C", tmp, "status", "--porcelain", "--untracked-files=all"], capture_output=True, text=True, check=True
            ).stdout
            self.assertEqual(status.strip(), "?? .gitignore")  # neither the env file nor the state shows
            self.assertEqual((Path(tmp) / ".gitignore").read_text(encoding="utf-8"), "build/\n")
            (paths["state"] / "harness.sqlite3").write_bytes(b"x")
            status = subprocess.run(["git", "-C", tmp, "status", "--porcelain"], capture_output=True, text=True).stdout
            self.assertNotIn(".harness", status)

    def test_a_linked_worktree_excludes_into_the_shared_git_dir(self) -> None:
        import subprocess

        with TemporaryDirectory() as tmp:
            repo, linked = Path(tmp) / "repo", Path(tmp) / "linked"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
            subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(linked)], check=True)
            init_workspace(linked)
            status = subprocess.run(["git", "-C", str(linked), "status", "--porcelain"], capture_output=True, text=True).stdout
            self.assertEqual(status.strip(), "")

    def test_load_sets_the_workspace_and_a_real_environment_variable_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            init_workspace(Path(tmp))
            (Path(tmp) / ENV_FILE_NAME).write_text("HARNESS_CHAT_MODEL=from-file\nHARNESS_CHAT_PROVIDER=codex\n", encoding="utf-8")
            env = {"HARNESS_CHAT_MODEL": "from-shell"}
            root = load_workspace(tmp, environ=env)
            self.assertEqual(env["HARNESS_CHAT_MODEL"], "from-shell")
            self.assertEqual(env["HARNESS_CHAT_PROVIDER"], "codex")
            self.assertEqual(env["HARNESS_WORKSPACE"], str(root))
            with self.assertRaises(WorkspaceError):
                load_workspace(Path(tmp) / "missing", environ={})

    def test_config_defaults_state_into_the_workspace(self) -> None:
        with workspace() as (root, config):
            self.assertEqual(config.workspace_root, root.resolve())
            self.assertEqual(config.trace_db_path, root.resolve() / ".harness" / "harness.sqlite3")
            self.assertEqual(config.trace_root, root.resolve() / ".harness" / "traces")
            self.assertIsNone(config.role_profiles_file_for())

    def test_a_roles_file_in_the_state_folder_is_picked_up(self) -> None:
        with workspace() as (root, _):
            (root / ".harness" / "roles.toml").write_text("", encoding="utf-8")
            self.assertEqual(HarnessConfig.from_env().role_profiles_file_for(), root.resolve() / ".harness" / "roles.toml")

    def test_there_are_no_per_project_suffixes_any_more(self) -> None:
        with workspace({"HARNESS_CHAT_MODEL": "plain", "HARNESS_CHAT_MODEL_TESTPROJ": "suffixed"}) as (_, config):
            self.assertEqual(config.chat_candidates_for("TestProj")[0][1], "plain")

    def test_a_model_setting_changed_at_runtime_is_seen_at_once(self) -> None:
        with workspace({"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"}) as (_, config):
            self.assertEqual(config.chat_candidates_for(), (("claude", "haiku"),))
            with patch.dict(os.environ, {"HARNESS_CHAT_MODEL": "sonnet"}):
                self.assertEqual(config.chat_candidates_for(), (("claude", "sonnet"),))

    def test_problems_are_reported_for_a_loose_file_and_a_service_secret(self) -> None:
        with workspace({"TELEGRAM_BOT_TOKEN": "x", "HARNESS_TELEGRAM_ALLOWED_CHAT_IDS": "1"}) as (root, _):
            os.chmod(root / ENV_FILE_NAME, 0o644)
            problems = " ".join(env_file_problems(root))
            self.assertIn("chmod 600", problems)
            self.assertIn("TELEGRAM_BOT_TOKEN belongs in the Telegram service", problems)
            self.assertIn("HARNESS_TELEGRAM_ALLOWED_CHAT_IDS belongs", problems)
