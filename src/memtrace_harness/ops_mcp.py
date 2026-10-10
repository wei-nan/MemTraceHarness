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

- ``propose_scope_change``: asks to add a probe host, a log file or a job to the scope file. It changes
  nothing: it records a request, and the Harness edits the scope file only after the human taps confirm
  in the chat. Always available, so a project that declares nothing yet can still be given tools.

- ``propose_github_issue``: asks to open an issue in one of the project's own GitHub repos. It opens
  nothing: it records a request, and the Harness runs ``gh issue create`` only after the human has seen
  the full title and body and tapped confirm in the chat.

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

from memtrace_harness.scope import (
    SCOPE_CHANGE_KINDS,
    describe_scope_change,
    github_issue_repos,
    validate_github_issue,
    validate_scope_change,
)

PROTOCOL_VERSION = "2024-11-05"
PROBE_TIMEOUT_SECONDS = 45
MAX_ATTEMPTS = 5
BODY_PREVIEW_CHARS = 300
LOG_DEFAULT_LINES = 80
LOG_MAX_LINES = 300
JOB_REQUEST_TTL_SECONDS = 600


class OpsError(RuntimeError):
    pass


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
        "name": "propose_scope_change",
        "description": (
            "Ask the user to give this project another ops capability when the tool or job you need does "
            "not exist: kind=probe_host (value=hostname http_probe may request), kind=log_file "
            "(value=path relative to the project that read_log may read) or kind=job (name + one-line shell "
            "command that run_job may run, for example starting a preview server). This does NOT change "
            "anything: the user sees exactly what is proposed and a confirm button, and only a tap applies "
            "it. Say that a confirmation is waiting; the new tool or job exists from your next reply on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SCOPE_CHANGE_KINDS)},
                "value": _string_param("hostname (probe_host) or relative file path (log_file)"),
                "name": _string_param("job name (kind=job)"),
                "command": _string_param("single-line shell command (kind=job)"),
            },
            "required": ["kind"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_github_issue",
        "description": (
            "Ask the user to open a GitHub issue (repo must be one of this project's repos; the tool says "
            "which if you pass a wrong one). This does NOT open it: the user sees the full title and body "
            "and a confirm button, and only a tap creates the issue, whose URL is then reported to them. "
            "Say that a confirmation is waiting; do not say the issue exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": _string_param("owner/name of the repository"),
                "title": _string_param("one-line issue title"),
                "body": _string_param("issue body (markdown)"),
            },
            "required": ["repo", "title", "body"],
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
    ) -> None:
        self.project = project
        self.working_directory = working_directory.resolve()
        self.hosts = {h.lower() for h in hosts}
        self.logs = {Path(p).name: p for p in logs}
        self.jobs = jobs
        self.store = store
        self.github_repos = list(github_repos or [])

    def tool_names(self) -> set[str]:
        names = {"propose_scope_change"} if self.store is not None else set()
        if self.store is not None and self.github_repos:
            names.add("propose_github_issue")
        if self.hosts:
            names.add("http_probe")
        if self.logs:
            names |= {"read_log", "list_logs"}
        if self.jobs:
            names |= {"list_jobs", "run_job"}
        return names


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


def propose_scope_change(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("scope change requests are not available here")
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
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_scope_change_request(
        request_id=f"scope_{uuid.uuid4().hex[:10]}",
        project=ctx.project,
        change=change,
        summary=describe_scope_change(change),
        expires_at=expires,
    ):
        raise OpsError("could not record the request")
    return (
        f"Proposed: {describe_scope_change(change)}. The user now has a confirm button in the chat "
        "(valid 10 minutes); nothing changes until they tap it. After they confirm, the new capability "
        "is available from your next reply on."
    )


def propose_github_issue(ctx: OpsContext, arguments: dict[str, Any]) -> str:
    if ctx.store is None:
        raise OpsError("issue requests are not available here")
    try:
        issue = validate_github_issue(
            ctx.github_repos, arguments.get("repo"), arguments.get("title"), arguments.get("body")
        )
    except ValueError as exc:
        raise OpsError(str(exc)) from exc
    expires = (datetime.now(timezone.utc) + timedelta(seconds=JOB_REQUEST_TTL_SECONDS)).isoformat()
    if not ctx.store.create_github_issue_request(
        request_id=f"issue_{uuid.uuid4().hex[:10]}", project=ctx.project, issue=issue, expires_at=expires
    ):
        raise OpsError("could not record the request")
    return (
        f"Proposed issue in {issue['repo']}: {issue['title']}. The user now has a confirm button in the chat "
        "(valid 10 minutes); the issue is created only after they tap it, and its URL is reported to them."
    )


def call_tool(ctx: OpsContext, name: str, arguments: dict[str, Any]) -> str:
    if name not in ctx.tool_names():
        raise OpsError(f"unknown tool: {name}")
    if name == "http_probe":
        return http_probe(ctx, arguments)
    if name == "read_log":
        return read_log(ctx, arguments)
    if name == "list_logs":
        return "\n".join(f"{n}: {p}" for n, p in sorted(ctx.logs.items()))
    if name == "list_jobs":
        return "\n".join(f"{n}: {c}" for n, c in sorted(ctx.jobs.items()))
    if name == "propose_scope_change":
        return propose_scope_change(ctx, arguments)
    if name == "propose_github_issue":
        return propose_github_issue(ctx, arguments)
    return request_job(ctx, arguments)


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
        return ok({"tools": [t for t in TOOLS if t["name"] in ctx.tool_names()]})
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
    """Launch spec for this server for one project. With a store, even a project that declares nothing
    gets propose_scope_change, the way to get its first capability."""
    hosts, logs, jobs = scope.probe_hosts or [], scope.log_files or [], scope.jobs or {}
    if not (hosts or logs or jobs or trace_db_path):
        return None  # nothing declared and no store to record a proposal in: no tools at all
    env = {
        "HARNESS_OPS_PROJECT": scope.name,
        "HARNESS_OPS_CWD": str(scope.working_directory),
        "HARNESS_OPS_HOSTS": json.dumps(hosts),
        "HARNESS_OPS_LOGS": json.dumps(logs),
        "HARNESS_OPS_JOBS": json.dumps(jobs),
        "HARNESS_OPS_GITHUB_REPOS": json.dumps(github_issue_repos(scope)),
    }
    if trace_db_path:
        env["HARNESS_TRACE_DB"] = str(Path(trace_db_path).resolve())
    return {"command": python or sys.executable, "args": ["-m", "memtrace_harness.ops_mcp"], "env": env}


if __name__ == "__main__":
    serve()
