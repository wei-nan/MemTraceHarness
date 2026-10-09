"""`memtrace-telegram --env-file <file>`: run one bot in front of one Harness."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from memtrace_services.client import HarnessClient
from memtrace_services.envfile import file_mode_problem, load_env_file
from memtrace_services.telegram.botapi import BotApi
from memtrace_services.telegram.relay import start_relay
from memtrace_services.telegram.service import TelegramService, TelegramSettings

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memtrace-telegram",
        description="A Telegram bot in front of one MemTrace Harness (reached over its local API).",
    )
    parser.add_argument("--env-file", type=Path, required=True, help="This service's own env file (bot token, allowlist, Harness URL/token)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if not args.env_file.is_file():
            raise ValueError(f"{args.env_file} does not exist")
        problem = file_mode_problem(args.env_file)
        if problem:
            print(f"WARNING: {problem}", file=sys.stderr, flush=True)
        settings = TelegramSettings.from_values(load_env_file(args.env_file), env_file=args.env_file)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    service = TelegramService(
        settings, HarnessClient(settings.harness_url, settings.harness_token), BotApi(settings.bot_token, api_base=settings.api_base)
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    relay = start_relay(service)
    threads = [
        threading.Thread(target=service.poll_forever, args=(stop,), name="telegram-poll", daemon=True),
        threading.Thread(target=service.deliver_forever, args=(stop,), name="telegram-deliver", daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(f"Telegram service up: Harness at {settings.harness_url}, {len(settings.allowed_chat_ids)} allowed chat(s).", flush=True)
    while not stop.wait(1.0):
        pass
    if relay is not None:
        relay.shutdown()
    print("Telegram service stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
