import unittest
from unittest.mock import patch

from memtrace_harness.agy_mcp_check import check_antigravity_mcp, parse_mcp_list

LISTING = """NAME         TYPE   STATUS   COMMAND/URL
memtrace     http   enabled  https://m.example/mcp
taiwantrade  stdio  enabled  /v/bin/python -m memtrace_harness.taiwantrade_mcp
"""
ENV = {"HARNESS_TAIWANTRADE_API_KEY_FILE": "/k"}


def check(listing, **kw):
    with patch.dict("os.environ", ENV, clear=True), patch("sys.executable", "/v/bin/python"):
        return check_antigravity_mcp(
            kw.pop("url", "https://m.example/mcp"), run=lambda _e: listing, which=lambda _e: "/bin/agy", **kw
        )


class AgyMcpCheckTests(unittest.TestCase):
    def test_parse_keeps_command_with_spaces(self) -> None:
        servers = parse_mcp_list(LISTING)
        self.assertEqual(servers["taiwantrade"]["command"], "/v/bin/python -m memtrace_harness.taiwantrade_mcp")
        self.assertEqual(servers["memtrace"]["status"], "enabled")

    def test_matching_registration_has_no_problems(self) -> None:
        self.assertEqual(check(LISTING), [])

    def test_missing_server_gives_a_register_command_without_the_key(self) -> None:
        problems = check("NAME TYPE STATUS COMMAND/URL\n")
        self.assertTrue(any("no MCP server 'taiwantrade'" in p and "agy mcp add" in p for p in problems))
        self.assertTrue(any("'memtrace'" in p for p in problems))

    def test_disabled_and_wrong_target_are_reported(self) -> None:
        problems = check(LISTING.replace("taiwantrade  stdio  enabled", "taiwantrade  stdio  disabled"))
        self.assertTrue(any("not enabled" in p for p in problems))
        problems = check(LISTING.replace("/v/bin/python", "/other/python"))
        self.assertTrue(any("not the expected" in p for p in problems))

    def test_no_opt_in_or_no_agy_means_no_check(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(check_antigravity_mcp(None, run=lambda _e: "", which=lambda _e: "/bin/agy"), [])
        with patch.dict("os.environ", ENV, clear=True):
            self.assertEqual(check_antigravity_mcp(None, run=lambda _e: "", which=lambda _e: None), [])

    def test_list_failure_is_reported_not_raised(self) -> None:
        def boom(_e):
            raise RuntimeError("agy exploded")

        with patch.dict("os.environ", ENV, clear=True):
            problems = check_antigravity_mcp(None, run=boom, which=lambda _e: "/bin/agy")
        self.assertIn("agy exploded", problems[0])


if __name__ == "__main__":
    unittest.main()
