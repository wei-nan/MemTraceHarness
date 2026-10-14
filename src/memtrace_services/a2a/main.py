"""``memtrace-a2a --config <a2a.toml>``: the A2A front of one Harness (docs/a2a-peers.md)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import tomllib
from pathlib import Path

from memtrace_services.a2a.carrier import Carrier
from memtrace_services.a2a.config import LiveConfig, load_config, new_peer_token
from memtrace_services.a2a.gateway import PeerGateway
from memtrace_services.a2a.protocol import Dispatcher
from memtrace_services.a2a.server import ProfileCache, start_server
from memtrace_services.envfile import file_mode_problem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memtrace-a2a", description="A2A front of a MemTrace Harness.")
    parser.add_argument("--config", type=Path, help="a2a.toml: address, the Harness behind it, the allowed peers")
    parser.add_argument("--new-peer", metavar="NAME", help="print a fresh token for a peer and the config lines to add")
    args = parser.parse_args(argv)
    if args.new_peer:
        token, digest = new_peer_token()
        print(f"Give this token to the peer {args.new_peer!r} (it is not stored here):\n  {token}\n")
        print(f'Add to a2a.toml:\n[peers.{args.new_peer}]\ntoken_sha256 = "{digest}"')
        return 0
    if args.config is None:
        parser.error("--config is required")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        problem = file_mode_problem(args.config)
        if problem:
            print(f"WARNING: {problem}", file=sys.stderr, flush=True)
        config = load_config(args.config)
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    gateway = PeerGateway(config.harness_url, config.harness_peer_token)
    config = LiveConfig(config, gateway)  # links made on the status page join the file's, without a restart
    server = start_server(config, Dispatcher(config, gateway), profile=ProfileCache(gateway))
    print(f"A2A: {config.public_url}/.well-known/agent-card.json (links are also read from the Harness)", flush=True)
    stop = threading.Event()
    # Always running: a link to ask someone can appear on the status page at any time, and it also keeps the Harness told of
    # this service's address and of who it can reach.
    threading.Thread(target=Carrier(config, gateway).run, args=(stop,), name="a2a-carrier", daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
