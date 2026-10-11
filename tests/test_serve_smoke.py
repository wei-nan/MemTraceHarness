"""The real thing, as a person would start it: `memtrace-harness serve` and the Telegram service as
two operating-system processes, talking over loopback, with a stub Telegram Bot API and a fake
model CLI. Slow (a few seconds); it is what proves the processes start, talk and stop cleanly."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.init_workspace import init_workspace_command
from memtrace_harness.workspace import ENV_FILE_NAME, models_file, parse_env_file, update_model_settings, write_env_updates

REPO = Path(__file__).resolve().parent.parent
FAKE_CLAUDE = """#!/bin/sh
case "$1" in
  mcp) echo "memtrace: https://kb.example/mcp (HTTP) - ✔ Connected" ;;
  *) echo "我是假的模型：收到。" ;;
esac
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StubBotApi:
    """Just enough of api.telegram.org: one scripted incoming message, everything else recorded."""

    def __init__(self, chat_id: int, text: str) -> None:
        self.sent: list[dict] = []
        self.commands: list = []
        self._updates = [{"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}]
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                body = json.loads(self.rfile.read(length) or b"{}")
                method = self.path.rsplit("/", 1)[-1]
                result: object = True
                if method == "getUpdates":
                    result, stub._updates = stub._updates, []
                    if not result:
                        time.sleep(0.2)
                elif method == "sendMessage":
                    stub.sent.append(body)
                    result = {"message_id": 500 + len(stub.sent)}
                elif method == "setMyCommands":
                    stub.commands = body["commands"]
                payload = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ServeSmokeTests(TestCase):
    def _wait(self, predicate, seconds=20, what="condition"):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def test_two_processes_hold_a_conversation_and_stop_cleanly(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty-folder"  # empty, and not a git repository
            fake = Path(tmp) / "claude"
            fake.write_text(FAKE_CLAUDE, encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

            import argparse

            init_workspace_command(
                argparse.Namespace(directory=root, name="smoke", memtrace_workspace="ws_smoke", non_interactive=True, workspace_dir=None)
            )
            port = free_port()
            write_env_updates(
                root / ENV_FILE_NAME,
                {"HARNESS_API_PORT": str(port), "HARNESS_CLAUDE_COMMAND": str(fake), "HARNESS_SHUTDOWN_GRACE_SECONDS": "1"},
            )
            update_model_settings(models_file(root), {"HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "haiku"})
            token = parse_env_file(root / ENV_FILE_NAME)["HARNESS_API_TOKEN"]
            bot = StubBotApi(chat_id=4242, text="你好")
            self.addCleanup(bot.close)
            telegram_env = Path(tmp) / "telegram.env"
            write_env_updates(
                telegram_env,
                {
                    "TELEGRAM_BOT_TOKEN": "stub-token", "TELEGRAM_ALLOWED_CHAT_IDS": "4242",
                    "HARNESS_URL": f"http://127.0.0.1:{port}", "HARNESS_API_TOKEN": token,
                    "TELEGRAM_API_BASE": bot.base, "TELEGRAM_POLL_TIMEOUT": "1",
                },
            )
            # A clean environment: only a path with no `agy`, and nothing of this machine's secrets.
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "PYTHONPATH": str(REPO / "src"), "HARNESS_MESSAGE_QUIET_SECONDS": "0"}
            harness = subprocess.Popen(
                [sys.executable, "-m", "memtrace_harness", "-C", str(root), "serve"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp,
            )
            self.addCleanup(harness.stdout.close)
            self.addCleanup(lambda: harness.poll() is None and harness.kill())

            def healthy() -> bool:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=1) as resp:
                        return resp.status == 200
                except OSError:
                    return False

            self._wait(healthy, what="the Harness API")
            telegram = subprocess.Popen(
                [sys.executable, "-m", "memtrace_services.telegram.main", "--env-file", str(telegram_env)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=tmp,
            )
            self.addCleanup(telegram.stdout.close)
            self.addCleanup(lambda: telegram.poll() is None and telegram.kill())

            self._wait(lambda: any("我是假的模型" in m.get("text", "") for m in bot.sent), what="the model's reply in Telegram")
            texts = [m["text"] for m in bot.sent]
            self.assertTrue(any("✅ Harness 已啟動（smoke）" in t for t in texts), texts)  # the startup notice
            reply = next(m for m in bot.sent if "我是假的模型" in m["text"])
            self.assertEqual(reply["chat_id"], 4242)
            self.assertIn("🧩 claude/haiku", reply["text"])
            self.assertIn("status", {c["command"] for c in bot.commands})

            # the status page's data over the API, with the token
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/status", headers={"Authorization": f"Bearer {token}"}
            )
            with urllib.request.urlopen(request, timeout=10) as resp:
                status = json.loads(resp.read())
            self.assertEqual(status["projects"][0]["name"], "smoke")
            self.assertEqual(status["harness"]["workspace_dir"], str(root.resolve()))

            for process in (telegram, harness):
                process.send_signal(signal.SIGTERM)
            for process in (telegram, harness):
                self.assertEqual(process.wait(timeout=15), 0)
            harness_output = harness.stdout.read()
            self.assertIn("Harness serve loop stopped.", harness_output)
            self.assertNotIn(token, harness_output)  # secrets are never printed
