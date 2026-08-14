from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.cli_process import CliProcessRunner


class CliProcessRunnerTests(TestCase):
    def test_missing_executable_is_unavailable_without_shell(self) -> None:
        with TemporaryDirectory() as directory:
            result = CliProcessRunner().run(
                ["memtrace-harness-command-that-does-not-exist", "prompt"],
                cwd=Path(directory),
                timeout_seconds=1,
            )

        self.assertTrue(result.unavailable)
        self.assertIsNone(result.return_code)
        self.assertIn("not found", result.error)

    def test_timeout_terminates_the_process_group_and_reports_how_it_died(self) -> None:
        # The runner now actually waits for and reports the terminated process's real
        # exit status (negative = killed by that signal) instead of leaving
        # return_code as an unknown None — this is what makes kill_process_for_thread()
        # able to reliably terminate a hung CLI call from another thread (see
        # cli_process.py's thread-keyed process registry / the shutdown-safety work).
        with TemporaryDirectory() as directory:
            result = CliProcessRunner().run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=Path(directory),
                timeout_seconds=0.05,
            )

        self.assertTrue(result.timed_out)
        self.assertIsNotNone(result.return_code)
        self.assertLess(result.return_code, 0)  # killed by signal, not a normal exit
        self.assertIn("timed out", result.error)
