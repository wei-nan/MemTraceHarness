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


class PublishJobParsingTests(TestCase):
    def _scope(self, extra: str) -> ProjectScope:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "harness-scope.md"
        path.write_text(f"# s\n\n- workspace_id: ws_test\n{extra}", encoding="utf-8")
        return ProjectScope.from_file(path)

    def test_a_declared_job_can_be_the_publish_job(self) -> None:
        scope = self._scope("- job: preview_start = echo hi\n- publish_job: preview_start\n")
        self.assertEqual(scope.publish_job, "preview_start")

    def test_a_publish_job_that_is_not_declared_is_ignored(self) -> None:
        scope = self._scope("- publish_job: preview_start\n")
        self.assertIsNone(scope.publish_job)

    def test_no_publish_job_by_default(self) -> None:
        self.assertIsNone(self._scope("- job: preview_start = echo hi\n").publish_job)


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

    def test_a_new_job_must_be_a_short_parseable_call_not_inline_code(self) -> None:
        from memtrace_harness.scope import validate_scope_change

        bad = [
            "python3 -c 'print(1)'",
            "node -e \"console.log(1)\"",
            "echo " + "x" * 400,
            "echo 'unbalanced",
            "if then fi done",
            "bash scripts/missing.sh",
        ]
        for command in bad:
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_scope_change(self.tmp, "job", None, "j", command)
        self.assertEqual(validate_scope_change(self.tmp, "job", None, "j", "echo ok")["command"], "echo ok")

    def test_a_job_that_runs_a_project_script_checks_the_script_too(self) -> None:
        from memtrace_harness.scope import validate_scope_change

        scripts = self.tmp / "scripts"
        scripts.mkdir()
        (scripts / "good.sh").write_text("#!/bin/bash\nset -e\necho ok\n")
        (scripts / "broken.sh").write_text("#!/bin/bash\nif true; then\n")
        (scripts / "deletes.sh").write_text("#!/bin/bash\nrm -rf build\n")
        (scripts / "broken.py").write_text("def x(:\n")
        self.assertEqual(validate_scope_change(self.tmp, "job", None, "j", "bash scripts/good.sh")["name"], "j")
        for command in ("bash scripts/broken.sh", "bash scripts/deletes.sh", "python3 scripts/broken.py"):
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_scope_change(self.tmp, "job", None, "j", command)

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
