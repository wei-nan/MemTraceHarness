from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from memtrace_harness import github_ci, tool_registry
from memtrace_harness.ops_mcp import OpsContext, call_tool
from pathlib import Path


def _ctx(repos):
    return OpsContext(project="P", working_directory=Path("."), hosts=[], logs=[], jobs={}, github_repos=repos)


def _done(out="ok", code=0):
    return MagicMock(stdout=out, stderr="", returncode=code)


class GithubCiTests(unittest.TestCase):
    def test_is_a_read_tool_offered_only_with_a_declared_repo(self) -> None:
        self.assertEqual(tool_registry.get("github_ci").tier, tool_registry.READ)
        self.assertIn("github_ci", _ctx(["wei-nan/Beri"]).tool_names())
        self.assertNotIn("github_ci", _ctx([]).tool_names())

    def test_failed_log_runs_fixed_argv_and_keeps_the_end(self) -> None:
        with patch("memtrace_harness.github_ci.subprocess.run", return_value=_done("x" * 20000 + "TEST FAILED")) as run:
            out = call_tool(_ctx(["wei-nan/Beri"]), "github_ci", {"action": "failed_log", "run_id": "38226686973"})
        self.assertEqual(run.call_args.args[0], ["gh", "run", "view", "38226686973", "-R", "wei-nan/Beri", "--log-failed"])
        self.assertTrue(out.endswith("TEST FAILED"))

    def test_other_repos_and_odd_ids_are_refused(self) -> None:
        ctx = _ctx(["wei-nan/Beri"])
        with patch("memtrace_harness.github_ci.subprocess.run") as run:
            for args in (
                {"action": "view", "repo": "evil/repo", "run_id": "1"},
                {"action": "view", "run_id": "1; rm -rf /"},
                {"action": "rerun", "run_id": "1"},
                {"action": "list", "branch": "a b"},
            ):
                with self.assertRaises(tool_registry.OpsError):
                    call_tool(ctx, "github_ci", args)
            run.assert_not_called()

    def test_gh_failure_is_reported_to_the_model(self) -> None:
        with patch("memtrace_harness.github_ci.subprocess.run", return_value=_done("HTTP 404", 1)):
            with self.assertRaises(tool_registry.OpsError):
                call_tool(_ctx(["wei-nan/Beri"]), "github_ci", {"action": "view", "run_id": "1"})


if __name__ == "__main__":
    unittest.main()
