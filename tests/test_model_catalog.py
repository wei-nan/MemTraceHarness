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


class ModelCatalogTests(TestCase):
    def setUp(self) -> None:
        model_catalog.clear_cache()
        self.addCleanup(model_catalog.clear_cache)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def _codex_home(self, name: str, models: list[tuple[str, str]]) -> None:
        d = self.home / name
        d.mkdir()
        (d / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": s, "visibility": v} for s, v in models]}), encoding="utf-8"
        )

    def test_lists_what_each_cli_offers(self) -> None:
        self._codex_home(".codex", [("gpt-6-luna", "list"), ("gpt-reserve", "hide")])
        self._codex_home(".codex-william", [("gpt-6-sol", "list"), ("gpt-6-luna", "list")])
        config = MagicMock(antigravity_command="agy")

        with patch("memtrace_harness.model_catalog.CliProcessRunner.run", return_value=_result(AGY_OUTPUT)):
            catalog = model_catalog.provider_models(config, home=self.home)

        self.assertEqual(catalog["codex"], ["gpt-6-luna", "gpt-6-sol"])
        self.assertEqual(catalog["antigravity"], ["gemini-3.8-flash-high", "gemini-3.1-pro-high"])
        self.assertEqual(catalog["claude"], ["opus", "sonnet", "haiku"])

    def test_a_broken_cli_contributes_nothing_and_results_are_cached(self) -> None:
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "models_cache.json").write_text("not json", encoding="utf-8")
        config = MagicMock(antigravity_command="agy")

        with patch("memtrace_harness.model_catalog.CliProcessRunner.run", return_value=_result("", 1)) as run:
            first = model_catalog.provider_models(config, home=self.home)
            model_catalog.provider_models(config, home=self.home)

        self.assertEqual((first["codex"], first["antigravity"]), ([], []))
        self.assertEqual(run.call_count, 1)

    def test_status_menus_put_the_catalog_first_then_configured_extras(self) -> None:
        from memtrace_harness.cli import _merge_model_catalog

        with patch(
            "memtrace_harness.model_catalog.provider_models",
            return_value={"codex": ["gpt-6-luna", "gpt-5.6-luna"], "claude": ["opus"], "antigravity": []},
        ):
            merged = _merge_model_catalog(
                MagicMock(), {"codex": {"gpt-5.6-luna", "old-model"}, "claude": set(), "antigravity": {"g"}}
            )

        self.assertEqual(merged["codex"], ["gpt-6-luna", "gpt-5.6-luna", "old-model"])
        self.assertEqual(merged["claude"], ["opus"])
        self.assertEqual(merged["antigravity"], ["g"])


class ChatModelSaveTests(TestCase):
    def test_saving_takes_effect_in_the_running_process_at_once(self) -> None:
        import os

        from memtrace_harness.cli import update_chat_model_for_project

        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)
            Path(".env").write_text("HARNESS_CHAT_PROVIDER=claude\n", encoding="utf-8")
            scope = MagicMock()
            scope.name = "Proj"
            keys = ("HARNESS_CHAT_PROVIDER_PROJ", "HARNESS_CHAT_MODEL_PROJ")
            saved = {k: os.environ.pop(k, None) for k in keys}
            self.addCleanup(lambda: [os.environ.pop(k, None) for k in keys] and None)
            with patch("memtrace_harness.cli.load_project_index", return_value=[scope]):
                update_chat_model_for_project(MagicMock(), "Proj", "codex", "gpt-6-luna")

            self.assertEqual(os.environ["HARNESS_CHAT_PROVIDER_PROJ"], "codex")
            self.assertEqual(os.environ["HARNESS_CHAT_MODEL_PROJ"], "gpt-6-luna")
            self.assertIn("HARNESS_CHAT_MODEL_PROJ=gpt-6-luna", Path(".env").read_text(encoding="utf-8"))
            self.assertIsNotNone(saved)
