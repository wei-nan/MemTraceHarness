from unittest import TestCase
from unittest.mock import patch

from memtrace_harness.chat_tools_check import check_chat_tools, claude_memtrace_status


class _Config:
    def __init__(self, candidates, url):
        self._candidates, self.url = candidates, url

    def chat_candidates_for(self, project):
        return self._candidates

    def command_for(self, provider):
        return provider

    def chat_command(self, provider, model, prompt, memtrace_read=False):
        cmd = [provider, "exec"]
        if memtrace_read and self.url:
            cmd.append('mcp_servers.memtrace.url="x"')
        return cmd


class ChatToolsCheckTests(TestCase):
    def test_codex_without_memtrace_url_is_reported_once_per_model(self) -> None:
        cfg = _Config((("codex", "m"),), None)
        with patch.dict("os.environ", {"MEMTRACE_API_TOKEN": "t"}):
            problems = check_chat_tools(cfg, ["a", "b"], run=lambda _e: "")
        self.assertEqual(len(problems), 2)  # one per project: the label names the project
        self.assertIn("no MemTrace tools", problems[0])

    def test_codex_with_url_and_token_is_fine_but_missing_token_is_not(self) -> None:
        cfg = _Config((("codex", "m"),), "http://x")
        with patch.dict("os.environ", {"MEMTRACE_API_TOKEN": "t"}):
            self.assertEqual(check_chat_tools(cfg, ["a"], run=lambda _e: ""), [])
        with patch.dict("os.environ", {}, clear=True):
            self.assertIn("without a token", check_chat_tools(cfg, ["a"], run=lambda _e: "")[0])

    def test_claude_needs_a_connected_memtrace_server(self) -> None:
        cfg = _Config((("claude", "haiku"),), "http://x")
        ok = "memtrace: https://h/mcp (HTTP) - ✔ Connected\n"
        self.assertEqual(check_chat_tools(cfg, ["a"], run=lambda _e: ok), [])
        self.assertIn("not connected", check_chat_tools(cfg, ["a"], run=lambda _e: "memtrace: u - ! Needs authentication")[0])
        self.assertIn("no 'memtrace'", check_chat_tools(cfg, ["a"], run=lambda _e: "other: u - ✔ Connected")[0])

    def test_listing_failure_is_reported_not_raised(self) -> None:
        def boom(_e):
            raise RuntimeError("nope")

        cfg = _Config((("claude", "haiku"),), "http://x")
        self.assertIn("could not list", check_chat_tools(cfg, ["a"], run=boom)[0])

    def test_status_parser_ignores_other_servers(self) -> None:
        self.assertIsNone(claude_memtrace_status("memtrace-old: u - ✔ Connected"))
