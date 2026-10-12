"""`memtrace-web --config <web.toml>`: the status page over one or more Harnesses."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import tomllib
from pathlib import Path

from memtrace_services.envfile import file_mode_problem
from memtrace_services.web.aggregate import Aggregator, HarnessTarget
from memtrace_services.web.chat import ChatHub
from memtrace_services.web.server import start_server

EXAMPLE = """\
host = "127.0.0.1"
port = 8787

[[harness]]
name = "my-project"
url = "http://127.0.0.1:8800"
token = "<that Harness's HARNESS_API_TOKEN>"
"""


def load_config(path: Path) -> tuple[str, int, list[HarnessTarget]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    targets = [
        HarnessTarget(name=str(h["name"]), url=str(h["url"]), token=str(h["token"]))
        for h in data.get("harness", [])
    ]
    if not targets:
        raise ValueError(f"{path} lists no [[harness]]; example:\n{EXAMPLE}")
    return str(data.get("host", "127.0.0.1")), int(data.get("port", 8787)), targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memtrace-web", description="Status page over MemTrace Harnesses.")
    parser.add_argument("--config", type=Path, required=True, help="web.toml: host, port and the Harnesses to show")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        problem = file_mode_problem(args.config)
        if problem:
            print(f"WARNING: {problem}", file=sys.stderr, flush=True)
        host, port, targets = load_config(args.config)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    aggregator = Aggregator(targets)
    aggregator.start()
    chat = ChatHub(targets)
    chat.start()
    server = start_server(host, port, aggregator, chat)
    print(f"Status page: http://{host}:{port} ({len(targets)} Harness(es))", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()
    aggregator.stop()
    chat.stop()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
