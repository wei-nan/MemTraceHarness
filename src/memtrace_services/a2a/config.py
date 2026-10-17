"""``a2a.toml``: this service's address, the Harness behind it, and the peers allowed to call it."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

EXAMPLE = """\
name = "my-project"
description = "Answers read-only questions about my-project from its knowledge base."
host = "127.0.0.1"
port = 8900
public_url = "http://127.0.0.1:8900"

[harness]
url = "http://127.0.0.1:8800"
peer_token = "<that Harness's HARNESS_PEER_API_TOKEN>"

[peers.beri]
token_sha256 = "<from: memtrace-a2a --new-peer beri>"
trust = "collaborate"   # "ask" (default): read-only questions. "collaborate": may also report a problem to fix.
max_concurrent = 2
rate_per_min = 10

[outbound.beri]
url = "http://127.0.0.1:8901"
token = "<the token beri's operator gave this Harness>"
description = "What beri knows about."
collaborate = false     # true: the model may report issues to it (report_issue); it must trust this Harness that way
"""


PEER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_peer_token() -> tuple[str, str]:
    """(token to hand to the peer once, sha256 line to keep here). The token itself is never stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


TRUST_SKILLS = {"ask": ("ask",), "collaborate": ("ask", "collaborate")}


@dataclass(frozen=True)
class PeerEntry:
    name: str
    token_sha256: str
    skills: tuple[str, ...] = ("ask",)
    max_concurrent: int = 2
    rate_per_min: int = 10


@dataclass(frozen=True)
class OutboundPeer:
    """Another Harness this one may ask. `url` is its A2A base; the Agent Card is read from it."""

    name: str
    url: str
    token: str
    description: str = ""
    collaborate: bool = False  # this Harness may report issues to it (the other side must trust us that way)
    display_name: str = ""


@dataclass(frozen=True)
class A2aConfig:
    name: str
    description: str
    host: str
    port: int
    public_url: str
    harness_url: str
    harness_peer_token: str
    peers: dict[str, PeerEntry] = field(default_factory=dict)
    outbound: dict[str, OutboundPeer] = field(default_factory=dict)

    def authenticate(self, supplied: str) -> PeerEntry | None:
        """The peer whose token this is. Every entry is compared, so timing does not reveal which one matched."""
        digest = hash_token(supplied) if supplied else ""
        found: PeerEntry | None = None
        for peer in self.peers.values():
            if digest and hmac.compare_digest(digest, peer.token_sha256):
                found = peer
        return found


def _positive(value: object, default: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def load_config(path: Path) -> A2aConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    harness = data.get("harness") or {}
    if not harness.get("url") or not harness.get("peer_token"):
        raise ValueError(f"{path} needs [harness] url and peer_token; example:\n{EXAMPLE}")
    peers: dict[str, PeerEntry] = {}
    for name, entry in (data.get("peers") or {}).items():
        digest = str(entry.get("token_sha256", "")).strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"peer {name!r}: token_sha256 must be a 64-character hex SHA-256")
        trust = str(entry.get("trust") or "ask")
        if trust not in TRUST_SKILLS:
            raise ValueError(f"peer {name!r}: trust must be 'ask' or 'collaborate'")
        peers[name] = PeerEntry(
            name=name, token_sha256=digest, skills=tuple(entry.get("skills") or TRUST_SKILLS[trust]),
            max_concurrent=_positive(entry.get("max_concurrent"), 2),
            rate_per_min=_positive(entry.get("rate_per_min"), 10),
        )
    outbound: dict[str, OutboundPeer] = {}
    for name, entry in (data.get("outbound") or {}).items():
        if not PEER_NAME.match(name) or not entry.get("url") or not entry.get("token"):
            raise ValueError(f"outbound peer {name!r}: a lowercase name, url and token are required")
        outbound[name] = OutboundPeer(
            name=name, url=str(entry["url"]).rstrip("/"), token=str(entry["token"]),
            description=str(entry.get("description") or ""), collaborate=entry.get("collaborate") is True,
        )
    host, port = str(data.get("host", "127.0.0.1")), int(data.get("port", 8900))
    return A2aConfig(
        name=str(data.get("name") or "harness"),
        description=str(data.get("description") or "A Harness answering read-only questions."),
        host=host, port=port,
        public_url=str(data.get("public_url") or f"http://{host}:{port}").rstrip("/"),
        harness_url=str(harness["url"]), harness_peer_token=str(harness["peer_token"]), peers=peers, outbound=outbound,
    )


logger = logging.getLogger(__name__)


class LiveConfig:
    """The service's configuration as it is *now*: the file's links plus the ones made on the status page, which the Harness
    keeps and which are re-read at most every `ttl` seconds, so a pairing or a revocation takes effect without a restart.
    Everything else (name, address, the Harness to talk to) is the file's. On a name both have, the Harness's link wins, so a
    link revoked on the page cannot be kept alive by an old line in the file. If the Harness cannot be reached the last
    answer stands (the file's links alone before there is one)."""

    def __init__(self, base: A2aConfig, gateway=None, ttl: float = 10.0, clock=time.monotonic) -> None:
        self._base, self._gateway, self._ttl, self._clock = base, gateway, ttl, clock
        self._merged = base
        self._at: float | None = None
        self._lock = threading.Lock()

    def current(self, *, force: bool = False) -> A2aConfig:
        if self._gateway is None:
            return self._base
        with self._lock:
            now = self._clock()
            if force or self._at is None or now - self._at >= self._ttl:
                self._at = now
                try:
                    links = self._gateway.links()
                    peers = dict(self._base.peers)
                    for item in links.get("inbound", []):
                        trust = item.get("trust") if item.get("trust") in TRUST_SKILLS else "ask"
                        peers[item["name"]] = PeerEntry(
                            name=item["name"], token_sha256=str(item["token_sha256"]).lower(), skills=TRUST_SKILLS[trust]
                        )
                    outbound = dict(self._base.outbound)
                    for item in links.get("outbound", []):
                        outbound[item["name"]] = OutboundPeer(
                            name=item["name"], url=str(item["url"]).rstrip("/"), token=str(item["token"]),
                            collaborate=bool(item.get("collaborate")),
                            display_name=str(item.get("display_name") or ""),
                        )
                    self._merged = replace(self._base, peers=peers, outbound=outbound)
                except Exception:  # noqa: BLE001 — keep serving with what we had
                    logger.warning("could not read the Harness's peer links; using the last ones")
                    if force:
                        return replace(self._base, peers={}, outbound={})
            return self._merged

    @property
    def peers(self) -> dict[str, PeerEntry]:
        return self.current().peers

    @property
    def outbound(self) -> dict[str, OutboundPeer]:
        return self.current().outbound

    def authenticate(self, supplied: str) -> PeerEntry | None:
        return self.current(force=True).authenticate(supplied)

    def __getattr__(self, name: str):
        # Only reached for what is not defined above: the file's own, unchanging fields.
        return getattr(self._base, name)
