"""GitHub writes the chat model may *propose*, one table entry per action.

Why this exists: the chat model's CLI cannot get an approval for its own GitHub connector, so any write it
attempts is refused. Instead it calls ``propose_github_action`` (ops_mcp.py), which only records a request; the
Harness shows the exact action to the operator and runs ``gh`` itself after a confirm tap (service.py).

Adding an action is adding one ``ActionSpec`` to ``ACTIONS``: how its fields are validated, the ``gh`` argv, and
how it is shown. The request/confirm/expiry/double-tap plumbing is shared and does not change.

Enforced here: the repo must be one of the project's own (``github_issue_repos``); every field is bounded; the
argv is a list, never a shell string. Deliberately not offered: merging a PR, deleting or editing an issue,
changing repo settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 8000
MAX_COMMENT_CHARS = 4000
MAX_LABELS = 10
MAX_LABEL_CHARS = 50

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")


def github_issue_repos(scope: Any) -> list[str]:
    """The repos a project may act on: its own `github_repo` plus its repos' (nothing else to configure)."""
    repos = [scope.github_repo, *(repo.github_repo for repo in scope.repos.values())]
    return [repo for repo in dict.fromkeys(repos) if repo]


def _repo(allowed: list[str], value: object) -> str:
    wanted = str(value or "").strip()
    match = next((a for a in allowed if a.lower() == wanted.lower()), None)
    if not _REPO.match(wanted) or match is None:
        raise ValueError(f"repo must be one of: {', '.join(allowed) or '(none declared)'}")
    return match


def _number(value: object) -> int:
    try:
        number = int(str(value).strip().lstrip("#"))
    except ValueError as exc:
        raise ValueError("number must be the issue or pull request number") from exc
    if number < 1:
        raise ValueError("number must be the issue or pull request number")
    return number


def _line(value: object, name: str, limit: int, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} must not be empty")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{name} must be one line")
    if len(text) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    return text


def _text(value: object, name: str, limit: int, *, required: bool) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    return text


def _labels(value: object, name: str = "labels") -> list[str]:
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    labels = [str(item).strip() for item in items if str(item).strip()]
    if len(labels) > MAX_LABELS:
        raise ValueError(f"at most {MAX_LABELS} {name}")
    for label in labels:
        if len(label) > MAX_LABEL_CHARS or "," in label or "\n" in label or "\r" in label:
            raise ValueError(f"invalid label {label!r}")
    return list(dict.fromkeys(labels))


@dataclass(frozen=True)
class ActionSpec:
    name: str
    confirm_word: str  # the button reads "✅ 確認<confirm_word>"
    validate: Callable[[list[str], dict[str, Any]], dict[str, Any]]
    argv: Callable[[dict[str, Any]], list[str]]
    summary: Callable[[dict[str, Any]], str]
    view: str | None = None  # "issue" or "pr": look the number up so the operator sees its title


def _issue_create(allowed: list[str], a: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": _repo(allowed, a.get("repo")),
        "title": _line(a.get("title"), "title", MAX_TITLE_CHARS),
        "body": _text(a.get("body"), "body", MAX_BODY_CHARS, required=True),
        "labels": _labels(a.get("labels")),
    }


def _number_comment(required: bool) -> Callable[[list[str], dict[str, Any]], dict[str, Any]]:
    def validate(allowed: list[str], a: dict[str, Any]) -> dict[str, Any]:
        return {
            "repo": _repo(allowed, a.get("repo")),
            "number": _number(a.get("number")),
            "comment": _text(a.get("comment"), "comment", MAX_COMMENT_CHARS, required=required),
        }

    return validate


def _issue_label(allowed: list[str], a: dict[str, Any]) -> dict[str, Any]:
    add, remove = _labels(a.get("labels")), _labels(a.get("remove_labels"), "remove_labels")
    if not add and not remove:
        raise ValueError("give labels to add and/or remove_labels to remove")
    return {"repo": _repo(allowed, a.get("repo")), "number": _number(a.get("number")), "labels": add, "remove_labels": remove}


def _with_comment(base: list[str], p: dict[str, Any]) -> list[str]:
    return [*base, "--comment", p["comment"]] if p.get("comment") else base


def _ref(p: dict[str, Any]) -> str:
    return f"{p['repo']}#{p['number']}"


ACTIONS: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec(
            "issue_create", "開單", _issue_create,
            lambda p: [
                "gh", "issue", "create", "--repo", p["repo"], "--title", p["title"], "--body", p["body"],
                *[x for label in p["labels"] for x in ("--label", label)],
            ],
            lambda p: f"{p['repo']}：{p['title']}",
        ),
        ActionSpec(
            "issue_close", "關閉", _number_comment(False),
            lambda p: _with_comment(["gh", "issue", "close", str(p["number"]), "--repo", p["repo"]], p),
            lambda p: f"關閉 {_ref(p)}", view="issue",
        ),
        ActionSpec(
            "issue_reopen", "重新開啟", _number_comment(False),
            lambda p: _with_comment(["gh", "issue", "reopen", str(p["number"]), "--repo", p["repo"]], p),
            lambda p: f"重新開啟 {_ref(p)}", view="issue",
        ),
        ActionSpec(
            "issue_comment", "留言", _number_comment(True),
            lambda p: ["gh", "issue", "comment", str(p["number"]), "--repo", p["repo"], "--body", p["comment"]],
            lambda p: f"在 {_ref(p)} 留言", view="issue",
        ),
        ActionSpec(
            "issue_label", "調整標籤", _issue_label,
            lambda p: [
                "gh", "issue", "edit", str(p["number"]), "--repo", p["repo"],
                *[x for label in p["labels"] for x in ("--add-label", label)],
                *[x for label in p["remove_labels"] for x in ("--remove-label", label)],
            ],
            lambda p: f"調整 {_ref(p)} 的標籤", view="issue",
        ),
        ActionSpec(
            "pr_comment", "留言", _number_comment(True),
            lambda p: ["gh", "pr", "comment", str(p["number"]), "--repo", p["repo"], "--body", p["comment"]],
            lambda p: f"在 PR {_ref(p)} 留言", view="pr",
        ),
    )
}


def validate_action(allowed: list[str], action: object, arguments: dict[str, Any]) -> dict[str, Any]:
    """The normalized payload ({"action": name, ...fields}), or ValueError. Run again at the confirm tap."""
    spec = ACTIONS.get(str(action or ""))
    if spec is None:
        raise ValueError(f"action must be one of: {', '.join(ACTIONS)}")
    return {"action": spec.name, **spec.validate(allowed, arguments)}


def build_argv(payload: dict[str, Any]) -> list[str]:
    return ACTIONS[payload["action"]].argv(payload)


def summarize(payload: dict[str, Any]) -> str:
    return ACTIONS[payload["action"]].summary(payload)


def detail_lines(payload: dict[str, Any]) -> list[str]:
    """What the operator should see besides the summary: every free-text field in full."""
    lines: list[str] = []
    for key, label in (("title", "標題"), ("labels", "加上標籤"), ("remove_labels", "移除標籤"), ("comment", "留言"), ("body", "內文")):
        value = payload.get(key)
        if value:
            lines.append(f"{label}：{', '.join(value) if isinstance(value, list) else value}")
    return lines
