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

    def test_timeout_is_reported_without_claiming_an_exit_code(self) -> None:
        with TemporaryDirectory() as directory:
            result = CliProcessRunner().run(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                cwd=Path(directory),
                timeout_seconds=0.05,
            )

        self.assertTrue(result.timed_out)
        self.assertIsNone(result.return_code)
        self.assertIn("timed out", result.error)
