from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memtrace_harness.config import HarnessConfig
from tests.workspace_support import workspace


class ChatCommandMediaTests(unittest.TestCase):
    def _config(self) -> HarnessConfig:
        ctx = workspace({"HARNESS_API_TOKEN": "t"})
        _root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        return config

    def test_codex_gets_images_with_a_double_dash_before_the_prompt(self) -> None:
        command = self._config().chat_command("codex", "m", "PROMPT", images=["/a/1.jpg", "/a/2.jpg"])
        self.assertEqual(command[-5:], ["-i", "/a/1.jpg", "/a/2.jpg", "--", "PROMPT"])

    def test_claude_may_read_only_the_inbox(self) -> None:
        inbox = Path(tempfile.mkdtemp()).resolve() / "inbox"
        command = self._config().chat_command("claude", "haiku", "PROMPT", read_dirs=[str(inbox)])
        allowed = command[command.index("--allowedTools") + 1]
        self.assertIn(f"Read(/{inbox}/**)", allowed)
        self.assertLess(command.index("--allowedTools"), command.index("--print"))

    def test_without_attachments_nothing_changes(self) -> None:
        config = self._config()
        self.assertNotIn("-i", config.chat_command("codex", "m", "P"))
        self.assertNotIn("Read(", " ".join(config.chat_command("claude", "haiku", "P")))


if __name__ == "__main__":
    unittest.main()
