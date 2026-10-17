"""Which Harnesses may talk to this one, and which this one may talk to (docs/a2a-peers.md).

Kept by the Harness, in ``.harness/peer_links.json`` (atomic replace, mode 0600, off-limits to the Developer like the
allowed list), so the status page can change it and the A2A service picks the change up without a restart. Two
directions, each by name:

- ``inbound``: a peer that may call this Harness: the SHA-256 of the token it was given (the token itself is shown once, when
  the link is made, and never stored) and its trust: ``ask`` (read-only questions) or ``collaborate`` (it may also report a
  problem for this Harness to investigate and, after the operator's tap, fix).
- ``outbound``: a Harness this one may call: its A2A address, the token it gave us (stored, because the A2A service has
  to send it), and whether this Harness's model may report issues to it.

The A2A service tells the Harness its own public address (``endpoint``) so a pairing made on the status page knows where
to point the other side. A link in the A2A service's own file still works; this adds to it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

PEER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TRUSTS = ("ask", "collaborate")
FILE_NAME = "peer_links.json"

_LOCK = threading.Lock()  # one lock for every instance: several admin calls may land together


class LinkError(ValueError):
    """A change that is refused; the text is for the operator."""


def sanitize_name(raw: str) -> str:
    """A project's name as a peer name: lowercase letters, digits, - and _, at most 32."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (raw or "").strip().lower()).strip("-_")[:32].strip("-_")
    if not PEER_NAME.match(cleaned):
        raise LinkError(f"cannot use {raw!r} as a peer name (use letters and digits)")
    return cleaned


def harness_name(config, scope) -> str:
    """The name other Harnesses call this one by: the one set for it (`HARNESS_PEER_NAME`, read at call time so a rename on the
    status page shows at once), else its project's name, lowercased. A project name that cannot be made into one (no letters or
    digits) falls back to `harness`, so the Harness always has a name."""
    configured = os.getenv("HARNESS_PEER_NAME", getattr(config, "peer_name", None) or "").strip()
    if configured and PEER_NAME.match(configured):
        return configured
    try:
        return sanitize_name(scope.name)
    except LinkError:
        return "harness"


def links_path(config) -> Path:
    return Path(config.trace_db_path).parent / FILE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not PEER_NAME.match(name):
        raise LinkError("a peer name is lowercase letters, digits, - or _ (at most 32)")
    return name


def _check_trust(trust: str) -> str:
    if trust not in TRUSTS:
        raise LinkError("trust must be 'ask' or 'collaborate'")
    return trust


def _check_url(url: str) -> str:
    if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+(:\d+)?(/\S*)?$", url.strip()):
        raise LinkError("the address must be an http(s) URL")
    return url.strip().rstrip("/")


class PeerLinks:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---- file
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        for key in ("inbound", "outbound"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    # ---- who may call this Harness
    def create_inbound(self, name: str, trust: str) -> str:
        """Make (or re-make) the link for `name` and return its token, the only time it exists in clear. Re-making a link
        replaces the token, so the old one stops working."""
        name, trust = _check_name(name), _check_trust(trust)
        token = secrets.token_urlsafe(32)
        with _LOCK:
            data = self._load()
            data["inbound"][name] = {
                "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(), "trust": trust, "created_at": _now(),
            }
            self._save(data)
        return token

    def set_inbound_trust(self, name: str, trust: str) -> None:
        name, trust = _check_name(name), _check_trust(trust)
        with _LOCK:
            data = self._load()
            if name not in data["inbound"]:
                raise LinkError(f"no peer named {name!r} may call this Harness")
            data["inbound"][name]["trust"] = trust
            self._save(data)

    def revoke_inbound(self, name: str) -> bool:
        with _LOCK:
            data = self._load()
            removed = data["inbound"].pop(_check_name(name), None) is not None
            if removed:
                self._save(data)
            return removed

    # ---- whom this Harness may call
    def set_outbound(self, name: str, url: str, token: str, collaborate: bool) -> None:
        name, url = _check_name(name), _check_url(url)
        if not isinstance(token, str) or not token.strip() or len(token) > 256 or any(c.isspace() for c in token):
            raise LinkError("the token is missing or malformed")
        with _LOCK:
            data = self._load()
            data["outbound"][name] = {
                "url": url, "token": token.strip(), "collaborate": bool(collaborate), "created_at": _now(),
            }
            self._save(data)

    def set_outbound_collaborate(self, name: str, collaborate: bool) -> None:
        with _LOCK:
            data = self._load()
            if _check_name(name) not in data["outbound"]:
                raise LinkError(f"this Harness has no link to {name!r}")
            data["outbound"][name]["collaborate"] = bool(collaborate)
            self._save(data)

    def remove_outbound(self, name: str) -> bool:
        with _LOCK:
            data = self._load()
            removed = data["outbound"].pop(_check_name(name), None) is not None
            if removed:
                self._save(data)
            return removed

    # ---- the A2A service's own address
    def set_endpoint(self, url: str) -> None:
        url = _check_url(url)
        with _LOCK:
            data = self._load()
            if data.get("endpoint") != url:
                data["endpoint"] = url
                self._save(data)

    # ---- views
    def public_view(self) -> dict:
        """Everything the status page may show: no token, no hash."""
        data = self._load()
        return {
            "endpoint": data.get("endpoint"),
            "inbound": [
                {"name": n, "trust": v.get("trust", "ask"), "created_at": v.get("created_at")}
                for n, v in sorted(data["inbound"].items())
            ],
            "outbound": [
                {"name": n, "display_name": v.get("display_name", n), "url": v.get("url"), "collaborate": bool(v.get("collaborate")), "created_at": v.get("created_at")}
                for n, v in sorted(data["outbound"].items())
            ],
        }

    def snapshot_for_service(self) -> dict:
        """What the A2A service needs, secrets included. Only the peer-token route hands this out."""
        data = self._load()
        return {
            "inbound": [
                {"name": n, "token_sha256": v.get("token_sha256", ""), "trust": v.get("trust", "ask")}
                for n, v in sorted(data["inbound"].items())
            ],
            "outbound": [
                {"name": n, "display_name": v.get("display_name", n), "url": v.get("url", ""), "token": v.get("token", ""), "collaborate": bool(v.get("collaborate"))}
                for n, v in sorted(data["outbound"].items())
            ],
        }
