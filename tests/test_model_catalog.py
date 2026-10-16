from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness import model_catalog
from memtrace_harness.cli_process import ProcessResult


def _result(stdout: str, code: int = 0) -> ProcessResult:
    return ProcessResult(
        command=[], return_code=code, stdout=stdout, stderr="",
        started_at="2026-10-02T00:00:00+00:00", completed_at="2026-10-02T00:00:01+00:00", duration_ms=1,
    )


AGY_OUTPUT = (
    "Fetching available models...\n"
    "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
    "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
)


CURSOR_OUTPUT = (
    "Available models\n\n"
    "auto - Auto (current, default)\n"
    "gpt-5.6-sol-high - GPT-5.6 Sol 1M High\n"
    "claude-sonnet-5-thinking-high - Claude Sonnet 5 1M Thinking\n"
    "\nTip: use --model <id>\n"
)


class ModelCatalogTests(TestCase):
    def setUp(self) -> None:
        model_catalog.clear_cache()
        self.addCleanup(model_catalog.clear_cache)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        # never read the operator's real secrets file or reach the network from a unit test
        env = patch.dict("os.environ", {"HARNESS_GLOBAL_SECRETS_FILE": str(self.home / "no-such-secrets.env")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(patch.stopall)

    def _codex_home(self, name: str, models: list[tuple[str, str]]) -> None:
        d = self.home / name
        d.mkdir()
        (d / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": s, "visibility": v} for s, v in models]}), encoding="utf-8"
        )

    def test_lists_what_each_cli_offers(self) -> None:
        self._codex_home(".codex", [("gpt-6-luna", "list"), ("gpt-reserve", "hide")])
        self._codex_home(".codex-william", [("gpt-6-sol", "list"), ("gpt-6-luna", "list")])
        config = MagicMock(antigravity_command="agy", workspace_root=None)

        with patch("memtrace_harness.model_catalog.CliProcessRunner.run", return_value=_result(AGY_OUTPUT)):
            catalog = model_catalog.provider_models(config, home=self.home)

        self.assertEqual(catalog["codex"], ["gpt-6-luna", "gpt-6-sol"])
        self.assertEqual(catalog["antigravity"], ["gemini-3.8-flash-high", "gemini-3.1-pro-high"])
        self.assertEqual(catalog["claude"], list(model_catalog.CLAUDE_ALIASES))
        self.assertIn("claude-sonnet-5-5", catalog["claude"])
        self.assertIn("claude-haiku-5-5", catalog["claude"])

    def test_a_broken_cli_contributes_nothing_and_results_are_cached(self) -> None:
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "models_cache.json").write_text("not json", encoding="utf-8")
        config = MagicMock(antigravity_command="agy", workspace_root=None)

        with patch("memtrace_harness.model_catalog.CliProcessRunner.run", return_value=_result("", 1)) as run:
            first = model_catalog.provider_models(config, home=self.home)
            model_catalog.provider_models(config, home=self.home)

        self.assertEqual((first["codex"], first["antigravity"], first["cursor"]), ([], [], []))
        self.assertEqual(run.call_count, 2)  # agy + cursor-agent, once each: the second lookup is cached

    def test_cursor_models_come_from_its_list_models_output(self) -> None:
        config = MagicMock(cursor_command="cursor-agent", workspace_root=None)

        with patch("memtrace_harness.model_catalog.CliProcessRunner.run", return_value=_result(CURSOR_OUTPUT)):
            models = model_catalog._cursor_models(config)

        self.assertEqual(models, ["auto", "gpt-5.6-sol-high", "claude-sonnet-5-thinking-high"])

    def test_status_menus_put_the_catalog_first_then_configured_extras(self) -> None:
        from memtrace_harness.admin import merge_model_catalog as _merge_model_catalog

        with patch(
            "memtrace_harness.model_catalog.provider_models",
            return_value={"codex": ["gpt-6-luna", "gpt-5.6-luna"], "claude": ["opus"], "antigravity": []},
        ):
            config = MagicMock()
            config.workspace_root = None
            merged = _merge_model_catalog(
                config, {"codex": {"gpt-5.6-luna", "old-model"}, "claude": set(), "antigravity": {"g"}}
            )

        self.assertEqual(merged["codex"], ["gpt-6-luna", "gpt-5.6-luna", "old-model"])
        self.assertEqual(merged["claude"], ["opus"])
        self.assertEqual(merged["antigravity"], ["g"])


class ChatModelSaveTests(TestCase):
    def test_saving_takes_effect_in_the_running_process_at_once(self) -> None:
        import os

        from memtrace_harness import admin
        from memtrace_harness.workspace import models_file, read_model_settings
        from tests.workspace_support import workspace

        with workspace({"HARNESS_CHAT_PROVIDER": "claude"}) as (root, config):
            admin.update_chat_model(config, "codex", "gpt-6-luna")
            self.assertEqual(os.environ["HARNESS_CHAT_PROVIDER"], "codex")
            self.assertEqual(os.environ["HARNESS_CHAT_MODEL"], "gpt-6-luna")
            self.assertEqual(read_model_settings(models_file(root))["HARNESS_CHAT_MODEL"], "gpt-6-luna")
            self.assertEqual(config.chat_candidates_for()[0], ("codex", "gpt-6-luna"))


class ModelChainTests(TestCase):
    def test_a_custom_model_is_remembered_for_every_menu(self) -> None:
        from memtrace_harness import admin
        from tests.workspace_support import workspace

        with workspace({"HARNESS_CHAT_PROVIDER": "claude"}) as (root, config):
            admin.update_model_chain(config, "chat", [
                {"provider": "claude", "model": "claude-sonnet-6-0"},
                {"provider": "codex", "model": "gpt-9"},
                {"provider": "antigravity", "model": "gemini-9-high"},
            ])
            self.assertEqual(
                config.chat_candidates_for(),
                (("claude", "claude-sonnet-6-0"), ("codex", "gpt-9"), ("antigravity", "gemini-9-high")),
            )
            with patch("memtrace_harness.model_catalog.provider_models", return_value={}):
                merged = admin.merge_model_catalog(config, {"claude": set(), "codex": set(), "antigravity": set()})
            self.assertIn("claude-sonnet-6-0", merged["claude"])
            self.assertIn("gpt-9", merged["codex"])
            admin.update_model_chain(config, "chat", [{"provider": "claude", "model": "sonnet"}])
            self.assertEqual(config.chat_candidates_for(), (("claude", "sonnet"),))

    def test_role_profile_takes_any_number_of_fallbacks(self) -> None:
        import shutil
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from memtrace_harness import admin, role_profiles

        with tempfile.TemporaryDirectory() as tmp:
            roles = Path(tmp) / "roles.toml"
            shutil.copy(Path(role_profiles.__file__).with_name("default-role-profiles.toml"), roles)
            config = SimpleNamespace(role_profiles_file_for=lambda *a: roles, workspace_root=Path(tmp))
            admin.update_role_profile_chain(config, "review", [
                {"provider": "claude", "model": "claude-sonnet-5-5"},
                {"provider": "codex", "model": "gpt-5.6-luna"},
                {"provider": "antigravity", "model": "gemini-3.8-flash-high", "reasoning_effort": "high"},
                {"provider": "claude", "model": "claude-haiku-5-5"},
            ])
            profile = role_profiles.load_role_profiles(roles)["review"]
            self.assertEqual(
                [(c.provider, c.model) for c in profile.candidates],
                [("claude", "claude-sonnet-5-5"), ("codex", "gpt-5.6-luna"),
                 ("antigravity", "gemini-3.8-flash-high"), ("claude", "claude-haiku-5-5")],
            )
            self.assertEqual(role_profiles.load_role_profiles(roles)["plan"].model, role_profiles.load_role_profiles()["plan"].model)
            with self.assertRaises(ValueError):
                admin.update_role_profile_chain(config, "review", [
                    {"provider": "claude", "model": "m", "reasoning_effort": "bogus"}])
            admin.update_role_profile_chain(config, "review", [{"provider": "codex", "model": "x"}])
            self.assertEqual(role_profiles.load_role_profiles(roles)["review"].fallbacks, ())


class ModelListKeyTests(TestCase):
    def setUp(self) -> None:
        model_catalog.clear_cache()
        self.addCleanup(model_catalog.clear_cache)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.secrets = self.dir / "secrets.env"
        env = patch.dict("os.environ", {"HARNESS_GLOBAL_SECRETS_FILE": str(self.secrets)})
        env.start()
        self.addCleanup(env.stop)
        import os

        os.environ.pop("HARNESS_MODEL_LIST_ANTHROPIC_KEY", None)

    def test_no_key_means_no_http_call(self) -> None:
        with patch("memtrace_harness.model_catalog.urllib.request.urlopen") as urlopen:
            self.assertEqual(model_catalog._claude_models_from_api(self.dir), [])
        urlopen.assert_not_called()

    def test_global_file_key_lists_models_and_a_workspace_key_wins(self) -> None:
        from memtrace_harness.workspace import model_list_key

        self.secrets.write_text("HARNESS_MODEL_LIST_ANTHROPIC_KEY=sk-global\n", encoding="utf-8")
        self.assertEqual(model_list_key(self.dir), "sk-global")
        (self.dir / ".harness.env").write_text("HARNESS_MODEL_LIST_ANTHROPIC_KEY=sk-own\n", encoding="utf-8")
        self.assertEqual(model_list_key(self.dir), "sk-own")

        body = json.dumps({"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-5-5"}]}).encode()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = body
        with patch("memtrace_harness.model_catalog.urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(model_catalog._claude_models_from_api(self.dir), ["claude-sonnet-5-5", "claude-haiku-5-5"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url.split("?")[0], "https://api.anthropic.com/v1/models")
        self.assertEqual(request.get_method(), "GET")

    def test_a_failing_call_falls_back_quietly(self) -> None:
        self.secrets.write_text("HARNESS_MODEL_LIST_ANTHROPIC_KEY=sk-x\n", encoding="utf-8")
        with patch("memtrace_harness.model_catalog.urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(model_catalog._claude_models_from_api(self.dir), [])

    def test_the_key_never_reaches_a_child_process(self) -> None:
        from memtrace_harness.cli_process import child_env

        env = child_env(environ={"HARNESS_MODEL_LIST_ANTHROPIC_KEY": "sk-x", "PATH": "/bin"})
        self.assertNotIn("HARNESS_MODEL_LIST_ANTHROPIC_KEY", env)
