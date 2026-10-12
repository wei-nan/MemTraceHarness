"""Read-only inspection for the chat model's ops tools (operator decision, 2026-10-12).

The model may look at files, logs, SQLite databases, launchd services, processes and git state without a
confirm tap, because none of it changes anything. What keeps that safe, all enforced here:

- **Roots.** A path must resolve (symlinks followed) inside the project's own folder or a root the operator
  declared in the scope file (``read_roots``). Reading another Harness's workspace is therefore something that
  Harness has to declare; it is never implied.
- **No secrets.** Files that hold credentials are refused by name wherever they are (``.harness.env``, ``*.env``,
  ``web.toml``, key/pem files, ``.ssh``...), and every returned text is passed through ``redact`` so a token
  that landed in a log or a database row is masked.
- **Databases** are opened ``mode=ro`` with ``query_only`` and a SQLite authorizer that allows only SELECT, so
  there is no write path even for a crafted statement; a statement runs for at most a few seconds.
- **System state** is a fixed set of argv lists (no shell): launchd list/print, ps, and ``git`` status/log/diff.
"""

from __future__ import annotations

import fnmatch
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_OUTPUT_CHARS = 12000
MAX_CELL_CHARS = 400
MAX_ROWS = 200
DEFAULT_LINES = 120
MAX_LINES = 600
MAX_FILE_BYTES = 8 * 1024 * 1024
QUERY_SECONDS = 5
COMMAND_SECONDS = 20


class ReadError(RuntimeError):
    pass


_SECRET_NAMES = (
    ".harness.env", ".env", ".env.*", "*.env", "web.toml", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    ".netrc", ".npmrc", ".pypirc", "credentials*", "*secret*", "*api_key*", "*apikey*", "*.token", "*_token*",
    "service-account*.json",
)
_SECRET_ALLOWED = ("*.example", "*.sample", "*.template")
_SECRET_DIRS = {".ssh", ".aws", ".gnupg", ".kube", ".docker"}

_REDACTIONS = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "<telegram-token>"),
    (re.compile(r"\bmt_[A-Za-z0-9_-]{12,}\b"), "<memtrace-key>"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "<api-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "<github-token>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), "<api-key>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)((?:token|secret|passwd|password|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{8,})"), r"\1<redacted>"),
    (re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"), "<token-like>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def is_secret_path(path: Path) -> bool:
    if any(part in _SECRET_DIRS for part in path.parts):
        return True
    name = path.name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_ALLOWED):
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in _SECRET_NAMES)


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（共 {len(text)} 字，只保留開頭 {MAX_OUTPUT_CHARS} 字）"


def resolve_readable(raw: object, base: Path, roots: list[Path]) -> Path:
    """The real path inside an allowed root, or ReadError. `~` and relative paths are accepted."""
    text = str(raw or "").strip()
    if not text or "\x00" in text:
        raise ReadError("path is required")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = base / path
    resolved = path.resolve()
    if not any(resolved == root or root in resolved.parents for root in roots):
        shown = ", ".join(str(r) for r in roots)
        raise ReadError(f"{resolved} is outside the readable roots ({shown}); the project's scope file must declare it in read_roots")
    if is_secret_path(resolved):
        raise ReadError(f"{resolved.name} holds credentials and is never readable")
    return resolved


def _int(value: object, default: int, low: int, high: int, what: str) -> int:
    if value in (None, ""):
        return default
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError) as exc:
        raise ReadError(f"{what} must be a number") from exc


def read_file(path: Path, arguments: dict) -> str:
    if not path.is_file():
        raise ReadError(f"{path} is not a file")
    if path.stat().st_size > MAX_FILE_BYTES * 8:
        raise ReadError(f"{path} is too large to read here ({path.stat().st_size} bytes); use a database query or a tail")
    count = _int(arguments.get("lines"), DEFAULT_LINES, 1, MAX_LINES, "lines")
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - MAX_FILE_BYTES))
        data = handle.read()
    if b"\x00" in data[:4096]:
        raise ReadError(f"{path.name} looks binary; use query_db for a database")
    lines = data.decode("utf-8", errors="replace").splitlines()
    from_head = str(arguments.get("from") or "tail").lower() == "head"
    picked = lines[:count] if from_head else lines[-count:]
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    where = "first" if from_head else "last"
    return _clip(redact(f"{path} (last modified {modified}, {len(lines)} lines), {where} {len(picked)}:\n" + "\n".join(picked)))


def list_dir(path: Path) -> str:
    if not path.is_dir():
        raise ReadError(f"{path} is not a directory")
    rows = []
    for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))[:300]:
        if is_secret_path(child):
            rows.append(f"{child.name}  (credentials — hidden)")
            continue
        try:
            info = child.stat()
        except OSError:
            continue
        stamp = datetime.fromtimestamp(info.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M")
        rows.append(f"{'d' if child.is_dir() else '-'} {info.st_size:>10} {stamp}  {child.name}")
    return _clip("\n".join(rows) or "(empty)")


def _authorizer(action: int, *_args: object) -> int:
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def query_db(path: Path, sql: object) -> str:
    """Run one SELECT against a SQLite file, read-only. An empty statement lists tables and columns."""
    if not path.is_file():
        raise ReadError(f"{path} is not a file")
    conn = None
    # A WAL database whose Harness is stopped has no -shm/-wal for a read-only connection to attach to;
    # then nothing is writing it, so `immutable=1` reads the file as it is.
    for flags in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"file:{path}?{flags}", uri=True, timeout=3)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            break
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
                conn = None
            error = exc
    if conn is None:
        raise ReadError(f"cannot open {path.name}: {error}")
    try:
        conn.execute("PRAGMA query_only = 1")
        statement = str(sql or "").strip().rstrip(";")
        if not statement:  # trusted SQL of our own, before the authorizer is installed
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            lines = []
            for table in tables:
                columns = [r[1] for r in conn.execute(f'SELECT * FROM pragma_table_info("{table}")')]
                lines.append(f"{table}({', '.join(columns)})")
            return _clip("\n".join(lines) or "(no tables)")
        conn.set_authorizer(_authorizer)
        started = time.monotonic()
        conn.set_progress_handler(lambda: 1 if time.monotonic() - started > QUERY_SECONDS else 0, 10000)
        if not re.match(r"(?is)^\s*(select|with)\b", statement):
            raise ReadError("only a single SELECT (or WITH … SELECT) statement is allowed")
        try:
            cursor = conn.execute(statement)
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as exc:
            raise ReadError(f"query failed: {exc}") from exc
        header = [d[0] for d in (cursor.description or [])]
        more = len(rows) > MAX_ROWS
        out = ["\t".join(header)]
        for row in rows[:MAX_ROWS]:
            cells = []
            for value in row:
                text = "NULL" if value is None else str(value).replace("\n", "\\n").replace("\t", " ")
                cells.append(text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS] + "…")
            out.append("\t".join(cells))
        if more:
            out.append(f"…（只顯示前 {MAX_ROWS} 列；加 LIMIT／WHERE 縮小範圍）")
        return _clip(redact("\n".join(out)))
    finally:
        conn.close()


_LABEL = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_FILTER = re.compile(r"^[A-Za-z0-9._/@:=+ -]{0,80}$")
_GIT_SUB = {
    "status": ["status", "--short", "--branch"],
    "log": ["log", "--oneline", "--decorate", "-n", "20"],
    "diff_stat": ["diff", "--stat"],
    "branches": ["branch", "-vv"],
}


def _run(argv: list[str], cwd: Path | None = None) -> str:
    try:
        done = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=COMMAND_SECONDS, stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ReadError(f"timed out after {COMMAND_SECONDS}s") from exc
    except OSError as exc:
        raise ReadError(str(exc)) from exc
    return ((done.stdout or "") + (done.stderr or "")).strip()


def system_status(kind: object, argument: object, root: Path | None) -> str:
    kind, text = str(kind or ""), str(argument or "").strip()
    if kind == "launchd_list":
        if not _FILTER.match(text):
            raise ReadError("filter has characters that are not allowed")
        rows = [r for r in _run(["launchctl", "list"]).splitlines() if not text or text.lower() in r.lower()]
        return _clip(redact("PID\tExit\tLabel\n" + "\n".join(rows[:300])))
    if kind == "launchd_print":
        if not _LABEL.match(text):
            raise ReadError("label must be a launchd label such as com.memtraceharness.Beri")
        return _clip(redact(_run(["launchctl", "print", f"gui/{os.getuid()}/{text}"])))
    if kind == "processes":
        if not _FILTER.match(text):
            raise ReadError("filter has characters that are not allowed")
        rows = _run(["ps", "-axo", "pid,etime,%cpu,%mem,command"]).splitlines()
        picked = [rows[0]] + [r for r in rows[1:] if not text or text.lower() in r.lower()][:100]
        return _clip(redact("\n".join(picked)))
    if kind == "git":
        if root is None:
            raise ReadError("git needs a repository path inside the readable roots")
        sub = _GIT_SUB.get(text)
        if sub is None:
            raise ReadError(f"git argument must be one of: {', '.join(_GIT_SUB)}")
        return _clip(redact(_run(["git", "-C", str(root), *sub])))
    raise ReadError("kind must be one of: launchd_list, launchd_print, processes, git")
