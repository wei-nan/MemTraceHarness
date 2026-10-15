from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness import verification as v
from memtrace_harness.scope import ProjectScope


def fail(text: str, code: int = 1) -> dict:
    return {"command": "t", "exit_code": code, "timed_out": False, "passed": False, "stdout_tail": text, "stderr_tail": ""}


class ClassifyTests(TestCase):
    def test_runner_output_formats_name_their_failed_tests(self) -> None:
        text = (
            "FooTest > bar FAILED\nTest Case '-[X y]' failed (0.2 seconds).\nnot ok 3 - parses\n"
            "FAILED tests/a.py::t - boom\n--- FAIL: TestZ (0.00s)\n"
        )
        self.assertEqual(
            v.failed_tests(text),
            {"FooTest > bar", "-[X y]", "parses", "tests/a.py::t", "TestZ"},
        )

    def test_passed_missing_tool_sdk_and_timeout_are_not_regressions(self) -> None:
        ok = {"command": "t", "exit_code": 0, "passed": True}
        self.assertEqual(v.classify(ok, None)["status"], v.PASSED)
        for result in (
            fail("MISSING_TOOL: java"),
            fail("SDK location not found. Define ANDROID_HOME"),
            fail("sh: gradle: command not found", 127),
            {**fail(""), "timed_out": True, "exit_code": None},
        ):
            self.assertEqual(v.classify(result, None)["status"], v.COULD_NOT_RUN)

    def test_same_failures_as_the_base_are_baseline_only(self) -> None:
        out = v.classify(fail("A > a FAILED\n"), fail("A > a FAILED\nB > b FAILED\n"))
        self.assertEqual(out["status"], v.BASELINE_FAILING)

    def test_a_new_or_unnamed_failure_is_a_regression(self) -> None:
        self.assertEqual(v.classify(fail("A > a FAILED\nC > c FAILED"), fail("A > a FAILED"))["status"], v.REGRESSION)
        self.assertEqual(v.classify(fail("boom"), fail("A > a FAILED"))["status"], v.REGRESSION)
        self.assertEqual(v.classify(fail("A > a FAILED"), None)["status"], v.REGRESSION)
        self.assertEqual(v.classify(fail("A > a FAILED"), {"passed": True})["status"], v.REGRESSION)


class LoopModeScopeTests(TestCase):
    def _mode(self, line: str) -> str:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness-scope.md"
            path.write_text(f"- name: P\n- workspace_id: ws_x\n- working_directory: {tmp}\n{line}\n")
            return ProjectScope.from_file(path).loop_mode

    def test_loop_mode_defaults_to_full_and_accepts_lean(self) -> None:
        self.assertEqual(self._mode(""), "full")
        self.assertEqual(self._mode("- loop_mode: lean"), "lean")
        self.assertEqual(self._mode("- loop_mode: nonsense"), "full")
