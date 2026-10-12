"""The Harness's own list of jobs the user has said "always allow" (operator decision, 2026-10-12).

A job a chat model asks to run normally waits for the user's confirm tap. When the user taps "確認並之後都允許",
the Harness records the job's name **and the SHA-256 of its exact command** here, and from then on a request for
that same job with that same command runs without asking (and says so in the chat). Change one character of the
command and the entry no longer matches, so the user is asked again.

The same list holds the other two kinds of request the chat model can make — a GitHub action, a scope
change, a model change — bound as described in ``rule_for``. Only the Harness writes this file (``.harness/allowed.json``, atomic replace, mode 0600): the chat model has no
tool that touches it, and ``.harness`` is off-limits to the Developer. A command the command policy forbids
(sudo, deletion, system or security settings) is never added and never matches, even if the file is edited by hand.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from memtrace_harness.command_policy import forbidden_reason


def command_hash(command: str) -> str:
    return hashlib.sha256(command.strip().encode("utf-8")).hexdigest()


def rule_for(kind: str, payload: dict | None) -> tuple[str, str] | None:
    """(label, key) of what an "always allow" on a non-job request would cover, or None when the request is not
    one the allowlist can hold. What each kind is bound to:

    - ``external_action`` (GitHub): the action *type* in one repo (``issue_comment`` on ``owner/repo``) — its text
      and issue number differ every time, so an exact match would never repeat;
    - ``scope_change``: the exact change (kind, value, job name and command), and a job command the command policy
      forbids is never held;
    - ``model_change``: the exact target, provider and model.
    """
    data = payload or {}
    if kind == "external_action" and data.get("action") and data.get("repo"):
        label = f"GitHub {data['action']} @ {data['repo']}"
        return label, f"github|{data['action']}|{str(data['repo']).lower()}"
    if kind == "scope_change" and data.get("kind"):
        if data.get("command") and forbidden_reason(str(data["command"])):
            return None
        parts = [str(data.get(k) or "") for k in ("kind", "value", "name", "command")]
        return "設定變更 " + " ".join(p for p in parts if p), "scope|" + "|".join(parts)
    if kind == "tool_call" and data.get("tool"):
        args = json.dumps(data.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
        return f"工具 {data['tool']} {args}", f"tool|{data['tool']}|{args}"
    if kind == "model_change" and data.get("target") and data.get("model"):
        return (
            f"模型 {data['target']} → {data.get('provider')}/{data['model']}",
            f"model|{data['target']}|{data.get('provider')}|{data['model']}",
        )
    return None


class AllowList:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data.get("jobs", []) if isinstance(e, dict)] if isinstance(data, dict) else []

    def _save(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps({"jobs": entries}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(self.path)

    def entries(self) -> list[dict]:
        return self._load()

    def is_allowed(self, project: str, job_name: str, command: str) -> bool:
        if forbidden_reason(command):
            return False
        digest = command_hash(command)
        return any(
            e.get("project") == project and e.get("job") == job_name and e.get("sha256") == digest
            for e in self._load()
        )

    def add(self, project: str, job_name: str, command: str) -> bool:
        """Record the user's "always allow"; False when already there. Forbidden commands raise ValueError."""
        reason = forbidden_reason(command)
        if reason:
            raise ValueError(reason)
        with self._lock:
            entries = self._load()
            if any(e.get("project") == project and e.get("job") == job_name and e.get("sha256") == command_hash(command) for e in entries):
                return False
            entries = [e for e in entries if not (e.get("project") == project and e.get("job") == job_name)]
            entries.append({
                "project": project, "job": job_name, "sha256": command_hash(command),
                "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            self._save(entries)
            return True

    def remove(self, project: str, name: str) -> bool:
        """Drop a job entry by job name, or a rule entry by its label."""
        with self._lock:
            entries = self._load()
            kept = [e for e in entries if not (e.get("project") == project and name in {e.get("job"), e.get("label")})]
            if len(kept) == len(entries):
                return False
            self._save(kept)
            return True

    def is_rule_allowed(self, project: str, kind: str, payload: dict | None, rule: tuple[str, str] | None = None) -> bool:
        """`rule` overrides the default binding of a kind (a tool_call's tool may bind more loosely than its exact arguments)."""
        rule = rule or rule_for(kind, payload)
        if rule is None:
            return False
        digest = command_hash(rule[1])
        return any(
            e.get("project") == project and e.get("kind") == kind and e.get("sha256") == digest for e in self._load()
        )

    def add_rule(self, project: str, kind: str, payload: dict | None, rule: tuple[str, str] | None = None) -> str | None:
        """Record "always allow" for a GitHub / scope / model request; the label added, None when already there.
        ValueError when the request is not one a rule can cover."""
        rule = rule or rule_for(kind, payload)
        if rule is None:
            raise ValueError("這類要求不能加入允許清單")
        label, key = rule
        digest = command_hash(key)
        with self._lock:
            entries = self._load()
            if any(e.get("project") == project and e.get("kind") == kind and e.get("sha256") == digest for e in entries):
                return None
            entries.append({
                "project": project, "kind": kind, "label": label, "sha256": digest,
                "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            self._save(entries)
            return label
