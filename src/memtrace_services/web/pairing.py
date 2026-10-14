"""Pairing two Harnesses from the status page.

The page already talks to every Harness it lists with that Harness's own API token, so it can do both halves of a pairing:
ask the callee to make a link for the caller (which returns the new link's token, once), then give that token and the
callee's A2A address to the caller. The token goes from one Harness to the other through this process and nowhere else: it is
never sent to the browser, never logged, never stored here. A pairing across machines uses an *invite* instead: the callee's
page makes an invite string (which does contain the token, shown once) that the caller's page accepts.

A link is between *Harnesses*, so a Harness is called by its own name (the one it is given in its settings, `beri`), not by its
project's or its folder's: a Harness that works in several repos is still one name."""

from __future__ import annotations

import base64
import json
import re

from memtrace_services.client import HarnessError, HarnessUnavailable
from memtrace_services.web.aggregate import Aggregator

INVITE_PREFIX = "mtinv1:"
TRUSTS = ("ask", "collaborate")
PEER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class PairingError(RuntimeError):
    """A pairing that cannot be done; the text is for the operator."""


def peer_name(project: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (project or "").strip().lower()).strip("-_")[:32].strip("-_")
    if not PEER_NAME.match(cleaned):
        raise PairingError(f"「{project}」不能當作 Harness 的連線名稱（請用英數字）")
    return cleaned


def harness_name(aggregator: Aggregator, project: str) -> str:
    """The name the Harness serving `project` goes by on the peer network (its own setting; the project's name, lowercased, for a
    Harness that has not reported one)."""
    for item in aggregator.collect().get("projects", []):
        if item.get("name") == project and item.get("peer_name"):
            return str(item["peer_name"])
    return peer_name(project)


def _target(aggregator: Aggregator, project: str):
    target = aggregator.target_for(project)
    if target is None:
        raise PairingError(f"這個頁面上找不到專案「{project}」")
    return target


def _check_trust(trust: str) -> str:
    if trust not in TRUSTS:
        raise PairingError("信任等級只能是 ask 或 collaborate")
    return trust


def _endpoint(target, project: str) -> str:
    try:
        endpoint = target.client().peer_links().get("endpoint")
    except HarnessError as exc:
        raise PairingError(f"讀不到「{project}」的連線設定：{exc.message}") from None
    except HarnessUnavailable:
        raise PairingError(f"連不到「{project}」的 Harness") from None
    if not endpoint:
        raise PairingError(
            f"「{project}」的 A2A 服務還沒有回報位址：memtrace-a2a 沒有在跑，或這個 Harness 的 .harness.env 沒有設 HARNESS_PEER_API_TOKEN"
        )
    return endpoint


def _link(aggregator: Aggregator, caller: str, callee: str, trust: str) -> dict:
    """`caller` may now ask `callee`, at `trust`. Both halves, or neither."""
    if caller == callee:
        raise PairingError("不能把一個專案連到它自己")
    caller_target, callee_target = _target(aggregator, caller), _target(aggregator, callee)
    _endpoint(caller_target, caller)  # its A2A service must be running too: it carries the questions
    endpoint = _endpoint(callee_target, callee)
    caller_name, callee_name = harness_name(aggregator, caller), harness_name(aggregator, callee)
    if caller_name == callee_name:
        raise PairingError(f"這兩個 Harness 的名稱都是「{caller_name}」，請先在設定頁把其中一個改名")
    try:
        token = callee_target.client().admin("peer-link-inbound-create", {"name": caller_name, "trust": trust})["token"]
    except HarnessError as exc:
        raise PairingError(exc.message) from None
    except HarnessUnavailable:
        raise PairingError(f"連不到「{callee}」的 Harness") from None
    try:
        caller_target.client().admin(
            "peer-link-outbound-set",
            {"name": callee_name, "url": endpoint, "token": token, "collaborate": trust == "collaborate"},
        )
    except (HarnessError, HarnessUnavailable) as exc:
        try:  # half a pairing is worse than none: take the callee's side back
            callee_target.client().admin("peer-link-inbound-revoke", {"name": caller_name})
        except (HarnessError, HarnessUnavailable):
            pass
        message = exc.message if isinstance(exc, HarnessError) else f"連不到「{caller}」的 Harness"
        raise PairingError(f"沒有連成功（{message}），已把對方那一半撤回") from None
    return {"from": caller, "to": callee, "trust": trust}


def pair(aggregator: Aggregator, caller: str, callee: str, trust: str, *, both: bool = False, reverse_trust: str = "ask") -> dict:
    """`caller` may ask `callee` (at `trust`); with `both`, `callee` may also ask `caller` (at `reverse_trust`)."""
    links = [_link(aggregator, caller, callee, _check_trust(trust))]
    if both:
        links.append(_link(aggregator, callee, caller, _check_trust(reverse_trust)))
    return {"links": links}


def make_invite(aggregator: Aggregator, project: str, caller_name: str, trust: str) -> str:
    """For a caller this page cannot reach: a link for `caller_name` on `project`, as a string to carry over. The string contains
    the link's token; it is shown once and not kept."""
    target = _target(aggregator, project)
    endpoint = _endpoint(target, project)
    try:
        token = target.client().admin(
            "peer-link-inbound-create", {"name": peer_name(caller_name), "trust": _check_trust(trust)}
        )["token"]
    except HarnessError as exc:
        raise PairingError(exc.message) from None
    except HarnessUnavailable:
        raise PairingError(f"連不到「{project}」的 Harness") from None
    payload = {"v": 1, "name": harness_name(aggregator, project), "url": endpoint, "token": token, "collaborate": trust == "collaborate"}
    return INVITE_PREFIX + base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def accept_invite(aggregator: Aggregator, project: str, invite: str) -> dict:
    target = _target(aggregator, project)
    text = (invite or "").strip()
    if not text.startswith(INVITE_PREFIX):
        raise PairingError("這不是邀請碼（應以 mtinv1: 開頭）")
    try:
        data = json.loads(base64.urlsafe_b64decode(text[len(INVITE_PREFIX):].encode("ascii")).decode("utf-8"))
        name, url, token = str(data["name"]), str(data["url"]), str(data["token"])
    except (ValueError, KeyError, TypeError):
        raise PairingError("邀請碼壞掉了，請重新產生") from None
    if not PEER_NAME.match(name):
        raise PairingError("邀請碼裡的名稱不合規定")
    try:
        target.client().admin(
            "peer-link-outbound-set",
            {"name": name, "url": url, "token": token, "collaborate": data.get("collaborate") is True},
        )
    except HarnessError as exc:
        raise PairingError(exc.message) from None
    except HarnessUnavailable:
        raise PairingError(f"連不到「{project}」的 Harness") from None
    return {"name": name, "url": url, "collaborate": data.get("collaborate") is True}
