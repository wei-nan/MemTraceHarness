"""One conversation through both halves: a Telegram update in, the Harness's answer back out as a
Telegram message — real Harness API and service, real Telegram service, a fake Bot API and a fake
model CLI. Nothing here is mocked between the two processes' worth of code."""

import os
import stat
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.api import HarnessApi
from memtrace_harness.runtime import build_harness
from memtrace_harness.scope import ProjectScope
from memtrace_services.client import HarnessClient
from memtrace_services.telegram.service import TelegramService, TelegramSettings
from tests.test_telegram_service import ALLOWED, FakeBot, text_update
from tests.workspace_support import workspace

FAKE_CLAUDE = """#!/bin/sh
# A model CLI that answers every chat call the same way.
echo "我看到了：收到你的訊息。"
"""


class EndToEndTests(TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        fake = Path(self._tmp.name) / "claude"
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        ctx = workspace(
            {
                "HARNESS_API_TOKEN": "tok",
                "HARNESS_CLAUDE_COMMAND": str(fake),
                "HARNESS_CHAT_PROVIDER": "claude",
                "HARNESS_CHAT_MODEL": "haiku",
            }
        )
        self.root, config = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.config = replace(config, api_port=0)
        scope = ProjectScope.from_file(self.root / "harness-scope.md")
        self.parts = build_harness(self.config, scope)
        api = HarnessApi(self.parts.service, self.config, scope)
        server = api.start()
        self.addCleanup(api.stop)
        self.addCleanup(server.server_close)
        settings = TelegramSettings.from_values(
            {
                "TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_ALLOWED_CHAT_IDS": str(ALLOWED),
                "HARNESS_API_TOKEN": "tok", "HARNESS_URL": f"http://127.0.0.1:{server.server_address[1]}",
            },
            env_file=Path(self._tmp.name) / "telegram.env",
        )
        self.bot = FakeBot()
        self.telegram = TelegramService(
            settings, HarnessClient(settings.harness_url, settings.harness_token), self.bot
        )

    def _deliver_until(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not predicate():
            self.telegram.deliver_once(wait=0.5)
        self.assertTrue(predicate(), f"nothing arrived; bot saw {self.bot.sent}")

    def test_a_chat_message_gets_a_model_reply_in_the_same_chat(self) -> None:
        self.bot.updates = [[text_update(text="你好")]]
        self.assertEqual(self.telegram.poll_once(), 1)
        self._deliver_until(lambda: any("收到你的訊息" in text for _, text, _ in self.bot.sent))
        reply = next(text for _, text, _ in self.bot.sent if "收到你的訊息" in text)
        self.assertIn("🧩 claude/haiku", reply)
        self.assertTrue(self.bot.typing)  # the "typing…" hint reached the chat too
        # the conversation is in the Harness's own record
        turns = self.parts.trace_store.get_primary_session_turns(f"psess_{self.parts.scope.name}")
        self.assertEqual([t["speaker"] for t in turns], ["user", "assistant"])

    def test_an_approval_travels_out_with_buttons_and_a_tap_travels_back(self) -> None:
        req = self.parts.approval_manager.request_approval(
            conversation_id="conv_x", workspace="ws_other", working_directory=str(self.root),
            reason="git_push", proposed_action="git push",
        )
        self.parts.service.notify_approval_request(req)
        self._deliver_until(lambda: bool(self.bot.sent))
        chat, text, keyboard = self.bot.sent[0]
        self.assertIn(req.id, text)
        approve = keyboard[0][0]["callback_data"]
        message_id = 101
        # the delivery receipt is how the Harness learned which message carries the request
        self.assertEqual(self.parts.approval_manager.get_by_message_ref(f"tg:{ALLOWED}:{message_id}").id, req.id)
        self.bot.updates = [[{"update_id": 9, "callback_query": {
            "id": "cb", "data": f"reject:{req.id}", "message": {"chat": {"id": ALLOWED}, "message_id": message_id}}}]]
        self.telegram.poll_once()
        self.assertEqual(self.parts.approval_manager.get_request(req.id).status, "rejected")
        self.assertEqual(approve, f"approve:{req.id}")
        self._deliver_until(lambda: (ALLOWED, message_id) in self.bot.cleared)  # its buttons are removed

    def test_a_message_from_a_stranger_gets_no_answer_and_leaves_no_trace(self) -> None:
        self.bot.updates = [[text_update(chat_id=555, text="你好")]]
        self.telegram.poll_once()
        time.sleep(0.3)
        self.telegram.deliver_once()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.parts.trace_store.get_primary_session_turns(f"psess_{self.parts.scope.name}"), [])
