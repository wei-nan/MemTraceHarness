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


class ScopeChangeTests(TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "harness-scope.md"
        self.file.write_text(
            "# Harness scope\n\n- workspace_id: ws_x\n- working_directory: " + str(self.tmp) +
            "\n- probe_hosts: a.example.com\n- job: old = echo old\n"
        )

    def test_validation_rejects_unsafe_proposals(self) -> None:
        from memtrace_harness.scope import validate_scope_change

        for args in [
            ("probe_host", "http://x.example.com/path"), ("probe_host", "a b"),
            ("log_file", "/etc/passwd"), ("log_file", "../outside.log"), ("log_file", "a,b.log"),
            ("job", None, "bad name", "echo hi"), ("job", None, "ok", "echo a\necho b"), ("job", None, "ok", ""),
            ("unknown", "x"),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_scope_change(self.tmp, *args)

    def test_changes_are_written_once_and_keep_the_rest_of_the_file(self) -> None:
        from memtrace_harness.scope import apply_scope_change, validate_scope_change

        host = validate_scope_change(self.tmp, "probe_host", "B.Example.com")
        self.assertTrue(apply_scope_change(self.file, host))
        self.assertFalse(apply_scope_change(self.file, host))
        logfile = validate_scope_change(self.tmp, "log_file", "logs/x.log")
        self.assertTrue(apply_scope_change(self.file, logfile))  # no log_files line yet: appended
        job = validate_scope_change(self.tmp, "job", None, "old", "echo new")
        self.assertTrue(apply_scope_change(self.file, job))  # same name: replaced, not duplicated
        scope = ProjectScope.from_file(self.file)
        self.assertEqual(scope.probe_hosts, ["a.example.com", "b.example.com"])
        self.assertEqual(scope.log_files, ["logs/x.log"])
        self.assertEqual(scope.jobs, {"old": "echo new"})
        self.assertIn("# Harness scope", self.file.read_text())
