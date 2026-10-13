"""Operations tools for the chat model, exposed as a stdio MCP server.

Why this exists: the chat model's CLI sandbox has no network, so it cannot tell "the service is
down" from "my sandbox is offline", and it cannot look at a failing job or run it again. MCP
servers are spawned outside that sandbox, so this process does the looking for it — diagnosis and
re-running a job belong to the harness, not to the Agent Loop (which is for changing code).

Boundaries enforced here, from what the operator declared in the project's scope file:
- ``http_probe``: GET only, only hosts in ``probe_hosts``, redirects are reported, never followed,
  and only the status, timing, size and the start of the body are returned.
- ``read_log``: only files in ``log_files`` (paths resolved under the working directory), tail only.
- ``run_job``: only jobs declared as ``job: name = command``. It does not run anything: it records a
  request and the human gets a confirm button in the chat; the Harness runs the command only after
  that tap. ``list_jobs`` shows what exists.

- ``read_file`` / ``list_dir`` / ``query_db`` / ``system_status``: read-only, no confirm tap (ops_read.py): roots are the
  project plus its scope file's ``read_roots``, credential files are never readable, output is redacted, SQLite is
  opened read-only with a SELECT-only authorizer.

- ``propose_scope_change``: asks to add a probe host, a log file or a job to the scope file. It changes
  nothing: it records a request, and the Harness edits the scope file only after the human taps confirm
  in the chat. Always available, so a project that declares nothing yet can still be given tools.

- ``propose_github_action``: asks for one GitHub write (open / close / reopen / comment on an issue, adjust
  its labels, comment on a PR; the table is in github_actions.py). It does nothing: it records a request, and
  the Harness runs ``gh`` only after the human has seen the exact action and tapped confirm in the chat.

- ``github_ci``: read-only GitHub Actions views (list / view / failed_log) through the host's ``gh`` for the
  project's own repos (github_ci.py). Offered only when the project declares a ``github_repo``.

Run: ``python -m memtrace_harness.ops_mcp`` (JSON-RPC over stdin/stdout).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from memtrace_harness import capabilities, ops_read, tool_registry
from memtrace_harness.command_policy import REFUSAL_HINT, forbidden_reason
from memtrace_harness.github_actions import ACTIONS, github_issue_repos, summarize, validate_action
from memtrace_harness.model_changes import TARGETS as MODEL_TARGETS, summarize as summarize_model_change, validate_model_change
from memtrace_harness.scope import SCOPE_CHANGE_KINDS, describe_scope_change, validate_scope_change

PROTOCOL_VERSION = "2024-11-05"
PROBE_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 5
BODY_PREVIEW_CHARS = 300
LOG_DEFAULT_LINES = 80
LOG_MAX_LINES = 300
JOB_REQUEST_TTL_SECONDS = 600
# A request still waiting this long before it expires gets one reminder with fresh buttons.
JOB_REMINDER_LEAD_SECONDS = 180


OpsError = tool_registry.OpsError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _string_param(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "http_probe",
        "description": (
            "Request a URL from outside your sandbox and report what really happens: HTTP status, seconds, "
            "bytes, the start of the body, or the error (timeout, DNS, refused). Only hosts the operator "
            "listed for this project are allowed. attempts (1-5) repeats it to show how variable the "
            "response time is. Use it to tell a service that is down or slow from a script that is wrong."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": _string_param("http(s) URL, GET"), "attempts": _string_param("1-5, default 1")},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_log",
        "description": "Last lines of one of this project's log files (names from list_logs).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": _string_param("log file name"), "lines": _string_param("default 80, max 300")},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a text file (a log, a config, source) without asking the user: the last lines by default "
            "(from=head for the first lines). Paths must be inside this project or a root its scope file declares "
            "in read_roots; files that hold credentials (.harness.env, *.env, web.toml, keys) are never readable "
            "and tokens in the text are masked. Use list_dir to find files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": _string_param("absolute, ~/..., or relative to the project"),
                "lines": _string_param("default 120, max 600"),
                "from": _string_param("tail (default) or head"),
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dir",
        "description": "List a directory (type, size, modified time, name) inside this project or a declared read root.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": _string_param("absolute, ~/..., or relative to the project")},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_db",
        "description": (
            "Run one read-only SELECT against a SQLite database (for example a Harness's .harness/harness.sqlite3: "
            "turns, runs, job_requests, outbox, schedules, primary_sessions_hot_log...). Empty sql lists the tables "
            "and columns. Nothing can be written. At most 200 rows, long cells are cut, tokens are masked; use LIMIT/WHERE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"db": _string_param("path to the .sqlite3 file"), "sql": _string_param("a single SELECT; empty = list tables")},
            "required": ["db"],
            "additionalProperties": False,
        },
    },
    {
        "name": "system_status",
        "description": (
            "Read-only system state, no shell: kind=launchd_list (argument = optional filter, shows pid/exit code per "
            "service), launchd_print (argument = a launchd label), processes (argument = optional filter), or git "
            "(argument = status | log | diff_stat | branches; root = a repository inside the readable roots); device errors: "
            "simulator_log (booted iOS Simulator) and mac_log (argument = process name; last hour of error/fault lines), "
            "crash_reports (argument = app name; Mac, simulator and synced iPhone crash reports), device_crash_reports "
            "(argument = app@device, e.g. Beri@William; pulls crash logs from a paired iPhone via devicectl, device must be unlocked)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": _string_param("launchd_list | launchd_print | processes | git | simulator_log | mac_log | crash_reports | device_crash_reports"),
                "argument": _string_param("filter, label, git subcommand, or process/app name"),
                "root": _string_param("repository path (git only)"),
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_logs",
        "description": "The log files this project allows read_log to read.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_jobs",
        "description": "The named jobs this project allows run_job to run, with their commands.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "request_install",
        "description": (
            "Ask the user to install something the Harness is missing. Only a request: the user sees the source, the exact "
            "command and your reason, and only a tap installs it (every time; no always-allow). kind: pip, npm, brew (name, "
            "optional version), model_file (url, filename, sha256 — required), script (url of a shell script, downloaded "
            "and run with sh), git (url, name, optional ref and one-line install_command), skill (name, and content or url "
            "of a SKILL.md). Never for sudo — give the user that command to run themselves."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": _string_param("pip | npm | brew | model_file | script | git | skill"),
                "reason": _string_param("why it is needed (shown to the user)"),
                "name": _string_param("package / folder / skill name"),
                "version": _string_param("optional version"),
                "url": _string_param("https URL (model_file, script, git, skill)"),
                "filename": _string_param("model_file: file name to save as"),
                "sha256": _string_param("model_file: required SHA-256 (hex)"),
                "ref": _string_param("git: optional branch or tag"),
                "install_command": _string_param("git: optional one-line command run in the clone"),
                "content": _string_param("skill: the SKILL.md text"),
            },
            "required": ["kind", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_scope_change",
        "description": (
            "Ask the user to give this project another ops capability when the tool or job you need does "
            "not exist: kind=probe_host (value=hostname http_probe may request), kind=log_file "
            "(value=path relative to the project that read_log may read) or kind=job (name + one-line shell "
            "command that run_job may run, for example starting a preview server). A job command must be a "
            "short call: real logic lives in a script file in the project (scripts/<name>.sh) that already "
            "exists (you cannot write files; if it is missing, hand the writing to the development flow first), and "
            "the job only runs it. Inline interpreter code (python -c, node -e) and "
            "long one-liners are rejected, and values that change (a version, a branch) must be read inside "
            "the script, never hard-coded in the command. Reuse an existing job's name to replace it instead "
            "of adding a variant. Set run_after=true when the user's request is to add the job AND run it: "
            "one tap then adds it and runs it once. This does NOT change anything by itself: the user sees "
            "exactly what is proposed and a confirm button, and only a tap applies it. Say that a "
            "confirmation is waiting; the new tool or job exists from your next reply on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SCOPE_CHANGE_KINDS)},
                "value": _string_param("hostname (probe_host) or relative file path (log_file)"),
                "name": _string_param("job name (kind=job)"),
                "command": _string_param("short single-line shell command, e.g. `bash scripts/x.sh` (kind=job)"),
                "run_after": {
                    "type": "boolean",
                    "description": "kind=job: also run it once, in the same confirmation, as soon as it is added",
                },
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_github_action",
        "description": (
            "Ask the user to approve one GitHub write on this project's repos; your own GitHub connector cannot "
            "write from here, so use this instead. action: issue_create (repo, title, body, labels?), "
            "issue_close / issue_reopen (repo, number, comment?), issue_comment (repo, number, comment), "
            "issue_label (repo, number, labels? to add, remove_labels? to remove), pr_comment (repo, number, "
            "comment). This does NOT do it: the user sees the exact action and a confirm button, and only a "
            "tap runs it. Say that a confirmation is waiting; never say it is done."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "repo": _string_param("owner/name of the repository"),
                "number": _string_param("issue or pull request number"),
                "title": _string_param("one-line title (issue_create)"),
                "body": _string_param("markdown body (issue_create)"),
                "comment": _string_param("comment text"),
                "labels": {"type": "array", "items": {"type": "string"}, "description": "labels to add"},
                "remove_labels": {"type": "array", "items": {"type": "string"}, "description": "labels to remove"},
            },
            "required": ["action", "repo"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_model_change",
        "description": (
            "Ask the user to PERMANENTLY change the primary model of the chat, a background job (digest, archive, "
            "recall) or an Agent Loop role (controller, planner, planner-escalation, red-team, developer). Use it "
            "only when the user wants the new model from now on, not for one task (for one task the task marker "
            "carries model=). This does NOT change anything: the user sees the exact change and a confirm button, "
            "and only a tap writes it. Say that a confirmation is waiting; never say it is changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": list(MODEL_TARGETS)},
                "model": _string_param("model id, e.g. claude-opus-5-5, or provider/model"),
                "provider": {"type": "string", "enum": ["claude", "codex", "antigravity"]},
            },
            "required": ["target", "model"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_job",
        "description": (
            "Ask to run one of the project's named jobs again. This does NOT run it: the user gets a "
            "confirm/cancel button in the chat, the job runs only after they tap confirm, and its result "
            "is reported to them automatically. Say that a confirmation is waiting; do not say it ran."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"name": _string_param("job name from list_jobs")},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
)


class OpsContext:
    def __init__(
        self,
        *,
        project: str,
        working_directory: Path,
        hosts: list[str],
        logs: list[str],
        jobs: dict[str, str],
        store: Any | None = None,
        github_repos: list[str] | None = None,
        read_roots: list[str] | None = None,
    ) -> None:
        self.project = project
        self.working_directory = working_directory.resolve()
        # What the read-only tools may look at: this project, plus whatever the scope file declares.
        self.read_roots = [self.working_directory]
        for root in read_roots or []:
            resolved = Path(os.path.expanduser(root)).resolve()
            if resolved not in self.read_roots:
                self.read_roots.append(resolved)
        self.hosts = {h.lower() for h in hosts}
        self.logs = {Path(p).name: p for p in logs}
        self.jobs = jobs
        self.store = store
        self.github_repos = list(github_repos or [])

    def tool_names(self) -> set[str]:
        return {
            spec.name for spec in tool_registry.all_specs() if spec.available is None or spec.available(self)
        }


def http_probe(
    ctx: OpsContext,
    arguments: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    url = str(arguments.get("url", ""))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OpsError("url must be an http(s) URL")
    if parsed.hostname.lower() not in ctx.hosts:
        raise OpsError(
            f"host {parsed.hostname} is not allowed for this project (allowed: {', '.join(sorted(ctx.hosts))})"
        )
    try:
        attempts = max(1, min(MAX_ATTEMPTS, int(arguments.get("attempts") or 1)))
    except ValueError as exc:
        raise OpsError("attempts must be a number from 1 to 5") from exc
    opener = opener or urllib.request.build_opener(_NoRedirect).open
    lines = [f"GET {url}"]
    for number in range(1, attempts + 1):
        started = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "memtrace-harness-probe/1"})
        try:
            with opener(request, timeout=PROBE_TIMEOUT_SECONDS) as response:
                body = response.read()
                status, headers = response.status, response.headers
            result = f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            body, headers = exc.read(), exc.headers
            result = f"HTTP {exc.code}" + (f" -> {headers.get('Location')}" if exc.code in range(300, 400) else "")
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "DNS failure" if isinstance(reason, socket.gaierror) else type(reason).__name__
            lines.append(f"#{number}: {kind}: {reason} after {time.monotonic() - started:.1f}s")
            if number < attempts:
                sleep(1)
            continue
        elapsed = time.monotonic() - started
        preview = body[:BODY_PREVIEW_CHARS].decode("utf-8", errors="replace").replace("\n", " ")
        lines.append(f"#{number}: {result}, {elapsed:.1f}s, {len(body)} bytes, body starts: {preview}")
        if number < attempts:
            sleep(1)
    return "\n".join(lines)


def read_log(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name", ""))
    if name not in ctx.logs:
        raise OpsError(f"unknown log {name!r} (allowed: {', '.join(sorted(ctx.logs)) or 'none'})")
    try:
        count = max(1, min(LOG_MAX_LINES, int(arguments.get("lines") or LOG_DEFAULT_LINES)))
    except ValueError as exc:
        raise OpsError("lines must be a number") from exc
    path = (ctx.working_directory / ctx.logs[name]).resolve()
    if not path.is_file():
        raise OpsError(f"log file {ctx.logs[name]} does not exist")
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    return f"{ctx.logs[name]} (last modified {modified}), last {len(tail)} lines:\n" + "\n".join(tail)


def request_job(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    name = str(arguments.get("name", ""))
    if name not in ctx.jobs:
        raise OpsError(f"unknown job {name!r} (allowed: {', '.join(sorted(ctx.jobs)) or 'none'})")
    reason = forbidden_reason(ctx.jobs[name])
    if reason:
        raise OpsError(f"job {name} is never run for a model ({reason}). {REFUSAL_HINT} Give the user the command to run themselves.")
    if ctx.store is None:
        raise OpsError("job requests are not available here")
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    request_id = f"job_{uuid.uuid4().hex[:10]}"
    if not ctx.store.create_job_request(
        request_id=request_id, project=ctx.project, job_name=name, command=ctx.jobs[name], expires_at=expires
    ):
        raise OpsError("could not record the request")
    return (
        f"Requested job {name}. The user now has a confirm button in the chat (valid 10 minutes); "
        "it runs only after they tap it, and the result is reported to them automatically."
    )


def _similar_jobs(ctx: OpsContext, name: str) -> list[str]:
    """Declared jobs whose name is a prefix of the new one or the other way round (a variant of the same job)."""
    return sorted(n for n in ctx.jobs if n != name and (n.startswith(name) or name.startswith(n)))


def propose_scope_change(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("scope change requests are not available here")
    run_after = bool(arguments.get("run_after"))
    if run_after and str(arguments.get("kind", "")) != "job":
        raise OpsError("run_after only applies to kind=job")
    try:
        change = validate_scope_change(
            ctx.working_directory,
            str(arguments.get("kind", "")),
            arguments.get("value"),
            arguments.get("name"),
            arguments.get("command"),
        )
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    if change["kind"] == "job":
        similar = _similar_jobs(ctx, change["name"])
        if similar:
            raise OpsError(
                f"a similar job already exists: {', '.join(similar)}. If this is the same purpose, propose it again "
                f"with that exact name to replace it (and write the command so nothing that changes is hard-coded); "
                "otherwise choose a clearly different name."
            )
    summary = describe_scope_change(change)
    if run_after:
        change = {**change, "run_after": True}
        summary += "（新增後立即執行一次）"
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_scope_change_request(
        request_id=f"scope_{uuid.uuid4().hex[:10]}",
        project=ctx.project,
        change=change,
        summary=summary,
        expires_at=expires,
    ):
        raise OpsError("could not record the request")
    if run_after:
        return (
            f"Proposed: {summary}. The user now has one confirm button in the chat (valid 10 minutes); a single tap "
            "adds the job and runs it once, and the result is reported to them. Say that a confirmation is waiting; "
            "do not say it was added or run."
        )
    return (
        f"Proposed: {summary}. The user now has a confirm button in the chat "
        "(valid 10 minutes); nothing changes until they tap it. After they confirm, the new capability "
        "is available from your next reply on."
    )


def propose_github_action(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("GitHub action requests are not available here")
    try:
        payload = validate_action(ctx.github_repos, arguments.get("action"), arguments)
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    summary = summarize(payload)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_external_action_request(
        request_id=f"act_{uuid.uuid4().hex[:10]}", project=ctx.project, key=f"github:{payload['action']}:{summary}",
        summary=summary, payload=payload, expires_at=expires,
    ):
        raise OpsError("could not record the request")
    return (
        f"Proposed: {summary}. The user now has a confirm button in the chat (valid 10 minutes); it is done "
        "only after they tap it, and the result is reported to them."
    )


def propose_model_change(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("model change requests are not available here")
    try:
        payload = validate_model_change(arguments)
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    summary = summarize_model_change(payload)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_model_change_request(
        request_id=f"mdl_{uuid.uuid4().hex[:10]}", project=ctx.project, key=f"model:{summary}",
        summary=summary, payload=payload, expires_at=expires,
    ):
        raise OpsError("could not record the request")
    return (
        f"Proposed: {summary}. The user now has a confirm button in the chat (valid 10 minutes); it changes "
        "only after they tap it, and the result is reported to them."
    )


def _read_tool(ctx: OpsContext, name: str, arguments: dict[str, Any]) -> str:
    try:
        if name == "read_file":
            return ops_read.read_file(ops_read.resolve_readable(arguments.get("path"), ctx.working_directory, ctx.read_roots), arguments)
        if name == "list_dir":
            return ops_read.list_dir(ops_read.resolve_readable(arguments.get("path"), ctx.working_directory, ctx.read_roots))
        if name == "query_db":
            return ops_read.query_db(ops_read.resolve_readable(arguments.get("db"), ctx.working_directory, ctx.read_roots), arguments.get("sql"))
        root = None
        if str(arguments.get("kind")) == "git":
            root = ops_read.resolve_readable(arguments.get("root") or ".", ctx.working_directory, ctx.read_roots)
        return ops_read.system_status(arguments.get("kind"), arguments.get("argument"), root)
    except ops_read.ReadError as exc:
        raise OpsError(str(exc)) from exc


def request_install(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("install requests are not available here")
    try:
        payload = capabilities.validate_install(arguments)
    except capabilities.InstallError as exc:
        raise OpsError(str(exc)) from exc
    summary = capabilities.summarize(payload)
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_install_request(
        request_id=f"inst_{uuid.uuid4().hex[:10]}", project=ctx.project, summary=summary, payload=payload, expires_at=expires
    ):
        raise OpsError("could not record the request")
    return (
        f"Proposed: {summary}. The user now has a confirm button in the chat (valid 10 minutes) showing the source, what "
        "will run and why; nothing is installed until they tap it. Say that a confirmation is waiting; do not say it is "
        "installed. Using it afterwards still goes through the usual confirmations."
    )


def _request_tool_call(ctx: OpsContext, spec: tool_registry.ToolSpec, arguments: dict[str, Any]) -> str:
    """A `change`-tier tool: record the request; the handler runs only after the user's tap (service.py)."""
    if ctx.store is None:
        raise OpsError(f"{spec.name} needs the user's confirmation, which is not available here")
    try:
        args = spec.validate(ctx, arguments) if spec.validate else dict(arguments)
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    summary = spec.summarize(args) if spec.summarize else f"{spec.name} {json.dumps(args, ensure_ascii=False, sort_keys=True)}"
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_tool_call_request(
        request_id=f"tool_{uuid.uuid4().hex[:10]}", project=ctx.project, tool=spec.name, arguments=args,
        summary=summary, expires_at=expires,
    ):
        raise OpsError("could not record the request")
    return (
        f"Requested: {summary}. It changes something, so the user now has a confirm button in the chat "
        "(valid 10 minutes); it runs only after they tap it and the result is reported to them. Say that a "
        "confirmation is waiting; do not say it has been done."
    )


def call_tool(ctx: OpsContext, name: str, arguments: dict[str, Any]) -> str:
    spec = tool_registry.get(name)
    if spec is None or name not in ctx.tool_names():
        raise OpsError(f"unknown tool: {name}")
    if spec.tier == tool_registry.CHANGE:
        return _request_tool_call(ctx, spec, arguments)
    return spec.handler(ctx, arguments)


def run_confirmed_tool(ctx: OpsContext, name: str, arguments: dict[str, Any]) -> str:
    """Run a `change`-tier tool after the user's tap (or an allowed-list match). Only service.py calls this."""
    spec = tool_registry.get(name)
    if spec is None or spec.tier != tool_registry.CHANGE:
        raise OpsError(f"{name} is not a tool that can be confirmed")
    try:
        args = spec.validate(ctx, arguments) if spec.validate else dict(arguments)
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    return spec.handler(ctx, args)


def _register_builtin_tools() -> None:
    schemas = {t["name"]: t for t in TOOLS}
    R, P = tool_registry.READ, tool_registry.PROPOSE
    for name, tier, handler, available in (
        ("http_probe", R, http_probe, lambda c: bool(c.hosts)),
        ("read_log", R, read_log, lambda c: bool(c.logs)),
        ("list_logs", R, lambda c, a: "\n".join(f"{n}: {p}" for n, p in sorted(c.logs.items())), lambda c: bool(c.logs)),
        ("list_jobs", R, lambda c, a: "\n".join(f"{n}: {cmd}" for n, cmd in sorted(c.jobs.items())), lambda c: bool(c.jobs)),
        ("read_file", R, lambda c, a: _read_tool(c, "read_file", a), None),
        ("list_dir", R, lambda c, a: _read_tool(c, "list_dir", a), None),
        ("query_db", R, lambda c, a: _read_tool(c, "query_db", a), None),
        ("system_status", R, lambda c, a: _read_tool(c, "system_status", a), None),
        ("run_job", P, request_job, lambda c: bool(c.jobs)),
        ("request_install", P, request_install, lambda c: c.store is not None),
        ("propose_scope_change", P, propose_scope_change, lambda c: c.store is not None),
        ("propose_model_change", P, propose_model_change, lambda c: c.store is not None),
        ("propose_github_action", P, propose_github_action, lambda c: c.store is not None and bool(c.github_repos)),
    ):
        tool_registry.register(tool_registry.ToolSpec(name=name, schema=schemas[name], handler=handler, tier=tier, available=available))


_register_builtin_tools()
from memtrace_harness import browser  # noqa: E402,F401  (registers `browse`)
from memtrace_harness import github_ci  # noqa: E402,F401  (registers `github_ci`)


def handle_message(message: dict[str, Any], ctx: OpsContext) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if msg_id is None:
        return None

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "harness-ops", "version": "1"},
            }
        )
    if method == "ping":
        return ok({})
    if method == "tools/list":
        names = ctx.tool_names()
        return ok({"tools": [spec.schema for spec in tool_registry.all_specs() if spec.name in names]})
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            text = call_tool(ctx, str(params.get("name", "")), params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": text}]})
        except OpsError as exc:
            return ok({"content": [{"type": "text", "text": str(exc)}], "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def context_from_env(env: Any) -> OpsContext | None:
    project, cwd = env.get("HARNESS_OPS_PROJECT"), env.get("HARNESS_OPS_CWD")
    if not project or not cwd:
        return None
    store = None
    if env.get("HARNESS_TRACE_DB"):
        from memtrace_harness.trace_store import TraceStore

        store = TraceStore(Path(env["HARNESS_TRACE_DB"]))
    return OpsContext(
        project=project,
        working_directory=Path(cwd),
        hosts=json.loads(env.get("HARNESS_OPS_HOSTS") or "[]"),
        logs=json.loads(env.get("HARNESS_OPS_LOGS") or "[]"),
        jobs=json.loads(env.get("HARNESS_OPS_JOBS") or "{}"),
        store=store,
        github_repos=json.loads(env.get("HARNESS_OPS_GITHUB_REPOS") or "[]"),
        read_roots=json.loads(env.get("HARNESS_OPS_READ_ROOTS") or "[]"),
    )


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    ctx = context_from_env(os.environ)
    if ctx is None:
        return
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle_message(message, ctx)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()


def ops_server_spec(scope: Any, trace_db_path: Path | str | None, *, python: str | None = None) -> dict[str, Any] | None:
    """Launch spec for this server for one project. The read-only tools are always there; with a store a project that
    declares nothing also gets propose_scope_change, the way to get its first capability."""
    hosts, logs, jobs = scope.probe_hosts or [], scope.log_files or [], scope.jobs or {}
    env = {
        "HARNESS_OPS_PROJECT": scope.name,
        "HARNESS_OPS_CWD": str(scope.working_directory),
        "HARNESS_OPS_HOSTS": json.dumps(hosts),
        "HARNESS_OPS_LOGS": json.dumps(logs),
        "HARNESS_OPS_JOBS": json.dumps(jobs),
        "HARNESS_OPS_GITHUB_REPOS": json.dumps(github_issue_repos(scope)),
        "HARNESS_OPS_READ_ROOTS": json.dumps(getattr(scope, "read_roots", None) or []),
    }
    if trace_db_path:
        env["HARNESS_TRACE_DB"] = str(Path(trace_db_path).resolve())
    return {"command": python or sys.executable, "args": ["-m", "memtrace_harness.ops_mcp"], "env": env}


def ops_context_for_scope(scope: Any, store: Any | None) -> OpsContext:
    """The context a confirmed `change`-tier tool runs in, built from the project's scope the way the chat model's own is."""
    return OpsContext(
        project=scope.name, working_directory=scope.working_directory, hosts=scope.probe_hosts or [],
        logs=scope.log_files or [], jobs=scope.jobs or {}, store=store, github_repos=github_issue_repos(scope),
        read_roots=getattr(scope, "read_roots", None) or [],
    )


if __name__ == "__main__":
    serve()

