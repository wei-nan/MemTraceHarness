"""The final gate: after the work, one check by code, never by a model's judgement.

It answers two of the three questions in docs/operating-contract.md ("Safety envelope and project-owned
loops"): did the change stay inside what it may touch, and does it carry a secret. (The third, whether a
produced tool is confirmed, is not built yet.)

What changed comes from git when the task has a worktree (the diff against the commit it started from, plus new
files). A workspace without a git branch, or a project edited in place where the diff would include the
operator's own uncommitted work, uses the paths the Developer reported instead; a file changed but not reported
is not seen there, which is the price of having no version control to ask.

A finding is ``block`` (stops the task) or ``warn`` (reported, does not stop). A model may add findings; nothing
here lets a model remove one. The operator can waive a stopped task by quoting the fingerprint of exactly these
findings and contents, so a changed change needs a new waiver."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 200_000
MAX_FILES = 300
WAIVER_PREFIX = "放行 gate "

# Paths no task may touch, whatever the scope file says (the Harness's own files and credentials).
ALWAYS_OFF_LIMITS = (".harness", ".harness.env", ".env", ".env.*", "*.pem", "*.p12", "id_rsa", "id_ed25519", ".git")

# Templates that name the variables a project needs but hold no values.
SAFE_ENV_NAMES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})

_SECRETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Anthropic/OpenAI key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
)
# Formats that look like personal data but also match harmless text, so they warn rather than stop.
_PERSONAL: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Taiwan ID number", re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)")),
    ("Taiwan mobile number", re.compile(r"(?<!\d)09\d{2}-?\d{3}-?\d{3}(?!\d)")),
)


@dataclass(frozen=True)
class Finding:
    severity: str  # "block" or "warn"
    rule: str  # "boundary", "scope", "secret", "personal"
    path: str
    detail: str
    line: int | None = None

    def key(self) -> str:
        return f"{self.severity}|{self.rule}|{self.path}|{self.line}|{self.detail}"

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"[{'擋下' if self.severity == 'block' else '提醒'}] {where} — {self.detail}"


@dataclass
class Changes:
    source: str  # "git" or "reported"
    files: list[str] = field(default_factory=list)
    added: dict[str, list[tuple[int | None, str]]] = field(default_factory=dict)  # path -> (line, text)


@dataclass
class GateResult:
    changes: Changes
    findings: list[Finding]
    fingerprint: str

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": "REJECT" if self.blocked else "PASS",
            "reason_code": "security" if self.blocked else "none",
            "findings": [{"description": f.render(), "severity": f.severity, "rule": f.rule} for f in self.findings],
            "unverified_items": [] if self.changes.source == "git" else [
                "No git history for this workspace: only the files the Developer reported were checked."
            ],
            "confidence": 1.0,
            "files_checked": len(self.changes.files),
            "source": self.changes.source,
            "fingerprint": self.fingerprint,
        }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:4096]


def _read_lines(path: Path) -> list[tuple[int | None, str]]:
    try:
        data = path.read_bytes()[:MAX_FILE_BYTES]
    except OSError:
        return []
    if not _is_text(data):
        return []
    return [(number, line) for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1)]


def _relative(name: str, root: Path) -> str | None:
    """``name`` as a path inside ``root`` (or None when it points outside)."""
    candidate = Path(name)
    try:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        return resolved.relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def collect_git_changes(directory: Path, base_commit: str) -> Changes | None:
    """The files that differ from ``base_commit`` (committed or not) plus new untracked files, with the lines the
    task added. None when git cannot say."""
    names = _git(directory, "diff", "--name-only", base_commit)
    fresh = _git(directory, "ls-files", "--others", "--exclude-standard")
    if names.returncode != 0 or fresh.returncode != 0:
        return None
    files = sorted({n for n in [*names.stdout.splitlines(), *fresh.stdout.splitlines()] if n.strip()})[:MAX_FILES]
    changes = Changes(source="git", files=files)
    diff = _git(directory, "diff", "-U0", "--no-color", base_commit)
    current: str | None = None
    line_no: int | None = None
    for raw in diff.stdout.splitlines():
        if raw.startswith("+++ "):
            current = raw[6:] if raw.startswith("+++ b/") else None
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            line_no = int(match.group(1)) if match else None
        elif raw.startswith("+") and current:
            changes.added.setdefault(current, []).append((line_no, raw[1:]))
            line_no = line_no + 1 if line_no is not None else None
    for name in fresh.stdout.splitlines():
        if name.strip():
            changes.added[name] = _read_lines(directory / name)
    return changes


def collect_reported_changes(directory: Path, reported: list[str]) -> Changes:
    files: list[str] = []
    changes = Changes(source="reported")
    for name in reported[:MAX_FILES]:
        rel = _relative(name, directory)
        if rel is None:
            files.append(name)  # kept as given so the boundary check can name it
            continue
        files.append(rel)
        target = directory / rel
        if target.is_file():
            changes.added[rel] = _read_lines(target)
    changes.files = sorted(set(files))
    return changes


def _matches(path: str, pattern: str) -> bool:
    pattern = pattern.strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not pattern:
        return False
    base = path.rsplit("/", 1)[-1]
    if pattern.endswith("/"):
        return path.startswith(pattern) or f"/{pattern}" in f"/{path}"
    if not any(ch in pattern for ch in "*?["):
        return path == pattern or path.startswith(pattern + "/") or base == pattern
    regex = fnmatch.translate(pattern).replace(r".*.*", ".*")
    return bool(re.match(regex, path)) or bool(re.match(fnmatch.translate(pattern), base))


def _mask(text: str) -> str:
    return text[:4] + "…" + f"({len(text)} 字元)"


def check(
    changes: Changes,
    *,
    directory: Path,
    off_limits: list[str] | None = None,
    declared_paths: list[str] | None = None,
) -> GateResult:
    findings: list[Finding] = []
    forbidden = [*ALWAYS_OFF_LIMITS, *(off_limits or [])]
    for path in changes.files:
        if _relative(path, directory) is None or path.startswith("../") or Path(path).is_absolute():
            findings.append(Finding("block", "boundary", path, "outside the task's working folder"))
            continue
        hit = None if path.rsplit("/", 1)[-1] in SAFE_ENV_NAMES else next((p for p in forbidden if _matches(path, p)), None)
        if hit:
            findings.append(Finding("block", "boundary", path, f"matches off-limits entry '{hit}'"))
            continue
        if declared_paths and not any(_matches(path, p) for p in declared_paths):
            findings.append(Finding("block", "scope", path, "outside the paths the Controller declared for this task"))
    for path, lines in changes.added.items():
        for number, text in lines:
            for name, pattern in _SECRETS:
                match = pattern.search(text)
                if match:
                    findings.append(Finding("block", "secret", path, f"looks like a {name} ({_mask(match.group(0))})", number))
            for name, pattern in _PERSONAL:
                match = pattern.search(text)
                if match:
                    findings.append(Finding("warn", "personal", path, f"looks like a {name} ({_mask(match.group(0))})", number))
    findings = sorted(set(findings), key=lambda f: (f.severity != "block", f.path, f.line or 0, f.rule))[:60]
    digest = hashlib.sha256()
    for finding in findings:
        digest.update(finding.key().encode())
    for path in sorted(changes.added):
        digest.update(path.encode())
        digest.update("\n".join(text for _, text in changes.added[path]).encode("utf-8", "replace"))
    return GateResult(changes=changes, findings=findings, fingerprint=digest.hexdigest()[:12])


def waived(goal: str, result: GateResult) -> bool:
    """True when the operator's answer quotes this exact result's fingerprint."""
    return f"{WAIVER_PREFIX}{result.fingerprint}" in (goal or "")


def summary_text(result: GateResult) -> str:
    blocking = [f for f in result.findings if f.severity == "block"]
    lines = [f.render() for f in result.findings]
    head = (
        "最終 gate 擋下這個任務（程式檢查，不是模型判斷）：\n" if blocking
        else "最終 gate 通過，另有提醒：\n"
    )
    tail = (
        f"\n若確定沒問題，回覆「{WAIVER_PREFIX}{result.fingerprint}」放行這一次；內容一變，放行就失效。"
        if blocking else ""
    )
    note = "" if result.changes.source == "git" else "\n（此工作區沒有 git，只檢查了 Developer 回報的檔案。）"
    return head + "\n".join(lines) + note + tail
