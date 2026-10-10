import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from memtrace_harness import admin
from memtrace_harness.scope import ProjectScope
from memtrace_harness.workspace import models_file, parse_env_file, read_model_settings
from tests.workspace_support import workspace

PROFILES = (
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
    'model = "sonnet"\n'
)


def with_roles(root: Path) -> dict[str, str]:
    roles = root / ".harness" / "roles.toml"
    roles.write_text(PROFILES, encoding="utf-8")
    return {"HARNESS_ROLE_PROFILES_FILE": str(roles)}


class UpdateRoleProfileTests(TestCase):
    """The status page's role-model edit, at the level that matters: does the TOML file end up
    correct and are the guardrails (unknown provider, shared default, unknown profile) real."""

    def _config(self, *, own_file: bool):
        ctx = workspace()
        root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        if own_file:
            os.environ.update(with_roles(root))
        return root, config

    def test_updates_provider_and_model_preserving_comments_and_fallback(self) -> None:
        root, config = self._config(own_file=True)
        admin.update_role_profile(config, "controller", "claude", "opus")
        text = (root / ".harness" / "roles.toml").read_text(encoding="utf-8")
        self.assertIn('provider = "claude"\nmodel = "opus"', text)
        self.assertIn("# A decision-rationale comment that must survive the edit untouched.", text)
        self.assertIn('[[profiles.controller.fallbacks]]\nprovider = "claude"\nmodel = "sonnet"', text)
        self.assertIn('[profiles.planner]\nrole = "Planner"\nprovider = "claude"\nmodel = "sonnet"', text)

    def test_rejects_unknown_provider_and_a_model_with_a_quote(self) -> None:
        _, config = self._config(own_file=True)
        with self.assertRaises(ValueError):
            admin.update_role_profile(config, "controller", "openai", "gpt-5")
        with self.assertRaises(ValueError):
            admin.update_role_profile(config, "controller", "claude", 'sonnet"; x=1')

    def test_refuses_a_workspace_still_on_the_shared_default(self) -> None:
        _, config = self._config(own_file=False)
        with self.assertRaises(ValueError) as caught:
            admin.update_role_profile(config, "controller", "claude", "opus")
        self.assertIn("shared by every Harness", str(caught.exception))

    def test_rejects_unknown_profile_id(self) -> None:
        _, config = self._config(own_file=True)
        with self.assertRaises(ValueError):
            admin.update_role_profile(config, "red-team", "claude", "opus")


class DedicatedRoleProfilesTests(TestCase):
    def test_copies_the_default_into_the_state_folder_and_uses_it_at_once(self) -> None:
        with workspace() as (root, config):
            self.assertIsNone(config.role_profiles_file_for())
            dest = admin.create_dedicated_role_profiles_file(config)
            self.assertEqual(dest, root.resolve() / ".harness" / "roles.toml")
            self.assertIn("[profiles.controller]", dest.read_text(encoding="utf-8"))
            self.assertEqual(config.role_profiles_file_for(), dest)  # live, no restart
            self.assertNotIn("HARNESS_ROLE_PROFILES_FILE", parse_env_file(root / ".harness.env"))  # found by its place
            with self.assertRaises(ValueError):
                admin.create_dedicated_role_profiles_file(config)


class ModelSettingTests(TestCase):
    """Saved to `.harness.env` and effective in the running process at once (until 2026-10-02 only
    the file was written, so the page showed the old model right after "saved")."""

    def test_chat_model_is_written_and_effective_without_a_restart(self) -> None:
        with workspace({"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"}) as (root, config):
            admin.update_chat_model(config, "antigravity", "gemini-3.8-flash-high")
            saved = read_model_settings(models_file(root))
            self.assertEqual((saved["HARNESS_CHAT_PROVIDER"], saved["HARNESS_CHAT_MODEL"]), ("antigravity", "gemini-3.8-flash-high"))
            self.assertNotIn("HARNESS_CHAT_MODEL", parse_env_file(root / ".harness.env") if (root / ".harness.env").is_file() else {})
            self.assertEqual(config.chat_candidates_for()[0], ("antigravity", "gemini-3.8-flash-high"))

    def test_digest_and_recall_models_leave_chat_alone(self) -> None:
        with workspace({"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"}) as (root, config):
            admin.update_digest_model(config, "claude", "sonnet")
            admin.update_recall_model(config, "claude", "claude-sonnet-5-5")
            saved = read_model_settings(models_file(root))
            self.assertEqual(saved["HARNESS_CHAT_MODEL"], "haiku")  # the chat choice is still the one it was
            self.assertEqual(config.digest_candidates_for()[0], ("claude", "sonnet"))
            self.assertEqual(config.recall_candidates_for()[0], ("claude", "claude-sonnet-5-5"))

    def test_the_memory_archive_has_its_own_chain_that_defaults_to_the_digest_chain(self) -> None:
        with workspace({"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"}) as (root, config):
            admin.update_digest_model(config, "claude", "sonnet")
            self.assertEqual(config.archive_candidates_for()[0], ("claude", "sonnet"))  # nothing of its own yet

            admin.update_model_chain(
                config, "archive",
                [{"provider": "claude", "model": "claude-opus-5-5"}, {"provider": "codex", "model": "gpt-5.6-sol"}],
            )

            saved = read_model_settings(models_file(root))
            self.assertEqual(saved["HARNESS_ARCHIVE_MODEL"], "claude-opus-5-5")
            self.assertEqual(saved["HARNESS_ARCHIVE_FALLBACKS"], "codex/gpt-5.6-sol")
            self.assertEqual(config.archive_candidates_for(), (("claude", "claude-opus-5-5"), ("codex", "gpt-5.6-sol")))
            self.assertEqual(config.digest_candidates_for()[0], ("claude", "sonnet"))  # the digest is untouched
            self.assertEqual(config.chat_candidates_for()[0], ("claude", "haiku"))
            with self.assertRaises(ValueError):
                admin.update_model_chain(config, "memory", [{"provider": "claude", "model": "sonnet"}])

    def test_fallbacks_roundtrip_and_an_empty_string_clears_the_chain(self) -> None:
        with workspace({"HARNESS_DIGEST_PROVIDER": "claude", "HARNESS_DIGEST_MODEL": "sonnet"}) as (root, config):
            admin.update_digest_fallbacks(config, "codex/gpt-5.6-sol, claude/opus")
            self.assertEqual(
                config.digest_candidates_for(), (("claude", "sonnet"), ("codex", "gpt-5.6-sol"), ("claude", "opus"))
            )
            admin.update_digest_fallbacks(config, "")
            self.assertEqual(config.digest_candidates_for(), (("claude", "sonnet"),))
            with self.assertRaises(ValueError):
                admin.update_digest_fallbacks(config, "openai/gpt-5")
            with self.assertRaises(ValueError):
                admin.update_digest_fallbacks(config, "codex")
            admin.update_recall_fallbacks(config, "codex/gpt-5.6-sol")
            with self.assertRaises(ValueError):
                admin.update_recall_fallbacks(config, "openai/gpt-5")

    def test_rejects_unknown_provider_and_empty_model(self) -> None:
        with workspace() as (_, config):
            with self.assertRaises(ValueError):
                admin.update_chat_model(config, "openai", "gpt-5")
            with self.assertRaises(ValueError):
                admin.update_chat_model(config, "claude", "   ")
            with self.assertRaises(ValueError):
                admin.update_recall_model(config, "openai", "x")


class StatusDataTests(TestCase):
    def test_known_models_collects_every_configured_pair(self) -> None:
        env = {"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "gemini-3.6-flash-high"}
        with workspace(env) as (root, config):
            # the real packaged default is a known-good example of the five-role shape
            default = Path(__file__).parent.parent / "src/memtrace_harness/default-role-profiles.toml"
            (root / ".harness" / "roles.toml").write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
            os.environ["HARNESS_ROLE_PROFILES_FILE"] = str(root / ".harness" / "roles.toml")
            scope = ProjectScope.from_file(root / "harness-scope.md")
            # No real CLI in unit tests: the provider catalog (agy models, Codex's cache) is stubbed.
            with patch("memtrace_harness.model_catalog.provider_models", return_value={}):
                data = admin.collect_status_data(config, scope)
        self.assertIn("gpt-5.6-luna", data["known_models"]["codex"])
        self.assertIn("sonnet", data["known_models"]["claude"])
        self.assertIn("gemini-3.6-flash-high", data["known_models"]["antigravity"])
        self.assertEqual(set(data["known_models"]), {"claude", "codex", "antigravity"})
        self.assertEqual([p["name"] for p in data["projects"]], ["ws"])  # the folder name
        self.assertEqual(data["harness"]["outbox_backlog"], 0)
        self.assertNotIn("bot", data["projects"][0]["dedicated"])  # nothing Telegram-shaped
        project = data["projects"][0]
        self.assertFalse(project["archive_is_own"])  # not set: it follows the digest chain
        self.assertEqual(project["archive_candidates"], project["digest_candidates"])
