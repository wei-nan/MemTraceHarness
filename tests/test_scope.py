from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.scope import ProjectScope


class AgentLoopEnabledParsingTests(TestCase):
    def _write_scope(self, tmp_path: Path, extra: str = "") -> Path:
        scope_path = tmp_path / "harness-scope.md"
        scope_path.write_text(
            f"# Harness scope — test\n\n- workspace_id: ws_test\n{extra}",
            encoding="utf-8",
        )
        return scope_path

    def test_defaults_to_enabled_when_field_absent(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope = ProjectScope.from_file(self._write_scope(tmp_path))
            self.assertTrue(scope.agent_loop_enabled)

    def test_disabled_value_turns_it_off(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope = ProjectScope.from_file(
                self._write_scope(tmp_path, "- agent_loop: disabled\n")
            )
            self.assertFalse(scope.agent_loop_enabled)

    def test_disabled_value_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope = ProjectScope.from_file(
                self._write_scope(tmp_path, "- agent_loop: Disabled\n")
            )
            self.assertFalse(scope.agent_loop_enabled)

    def test_any_other_value_stays_enabled(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            scope = ProjectScope.from_file(
                self._write_scope(tmp_path, "- agent_loop: enabled\n")
            )
            self.assertTrue(scope.agent_loop_enabled)
