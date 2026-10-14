from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from time import monotonic


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# What a child process (a provider CLI, a job, a verify command) may inherit. The Harness's own
# environment holds its secrets — the MemTrace token, its API token — and a model that runs shell
# commands (the Developer does) would otherwise be handed all of them. Everything not named here is
# dropped; HARNESS_PASSTHROUGH_ENV adds names (comma-separated) for the odd tool that needs one.
_CHILD_ENV_EXACT = frozenset(
    {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM", "TZ", "LANG", "LANGUAGE",
        "COLORTERM", "NO_COLOR", "FORCE_COLOR", "PWD", "SSH_AUTH_SOCK", "DISPLAY", "EDITOR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
        "no_proxy", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONUNBUFFERED",
        "CODEX_HOME", "GH_TOKEN", "GITHUB_TOKEN",
    }
)
_CHILD_ENV_PREFIXES = ("LC_", "XDG_", "CLAUDE_", "ANTHROPIC_", "GEMINI_", "GOOGLE_", "AGY_", "CODEX_", "OPENAI_")
# Only a child that was given the MemTrace MCP server on its command line gets the token it needs.
MEMTRACE_TOKEN_ENV = "MEMTRACE_API_TOKEN"


def child_env(
    command: list[str] | None = None, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a child process gets: the allowlist above, plus MEMTRACE_API_TOKEN only when
    the command wires the MemTrace MCP server to read it (Codex's bearer_token_env_var)."""
    source = os.environ if environ is None else environ
    extra = {n.strip() for n in source.get("HARNESS_PASSTHROUGH_ENV", "").split(",") if n.strip()}
    env = {
        key: value
        for key, value in source.items()
        if key in _CHILD_ENV_EXACT or key in extra or key.startswith(_CHILD_ENV_PREFIXES)
    }
    if (
        command
        and MEMTRACE_TOKEN_ENV in source
        and any(arg.startswith("mcp_servers.memtrace.") for arg in command)
        and any(MEMTRACE_TOKEN_ENV in arg for arg in command)
    ):
        env[MEMTRACE_TOKEN_ENV] = source[MEMTRACE_TOKEN_ENV]
    return env


@dataclass(frozen=True)
class ProcessResult:
    command: list[str]
    return_code: int | None
    stdout: str
    stderr: str
    started_at: str
    completed_at: str
    duration_ms: int
    timed_out: bool = False
    unavailable: bool = False
    cancelled: bool = False
    error: str | None = None


# Keyed by threading.get_ident(), not by any caller-supplied id: each Agent Loop
# background thread (see inflight.py) runs its stages one at a time, synchronously,
# in that same OS thread — so "the process this thread is currently blocked on" is
# always well-defined and unique. This lets kill_process_for_thread() reach into a
# running CLI call from a completely different thread (the Harness's shutdown-drain
# sequence) without threading a cancellation token through every layer of
# AgentLoopRunner/adapters/CliModelAdapter — the registry is the only extra plumbing.
_active_processes_lock = threading.Lock()
_active_processes: dict[int, dict] = {}


def _register_process(process: subprocess.Popen) -> int:
    ident = threading.get_ident()
    with _active_processes_lock:
        _active_processes[ident] = {"process": process, "cancelled": False}
    return ident


def _unregister_process(thread_ident: int) -> None:
    with _active_processes_lock:
        _active_processes.pop(thread_ident, None)


def _was_cancelled(thread_ident: int) -> bool:
    with _active_processes_lock:
        entry = _active_processes.get(thread_ident)
        return bool(entry and entry["cancelled"])


def kill_process_for_thread(thread_ident: int) -> bool:
    """Force-terminate whatever CLI subprocess the given thread is currently blocked
    on, including any children it spawned (killed via its whole process group, not
    just the direct child — a provider CLI may itself launch helper processes).
    Returns False if that thread has no tracked process (already finished, or never
    started one). Blocks the caller briefly (up to ~5s) to let SIGTERM be honored
    before escalating to SIGKILL — acceptable for a shutdown-drain caller that has
    already been waiting out a grace period."""
    with _active_processes_lock:
        entry = _active_processes.get(thread_ident)
        if not entry:
            return False
        entry["cancelled"] = True
        process: subprocess.Popen = entry["process"]
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
    return True


# A provider CLI is started with start_new_session=True, so it is its own process-group leader and can outlive the
# Harness process (a restart that is not a clean shutdown). The command a group leader runs names the provider.
_PROVIDER_CLI = re.compile(r"(^|[/\s])(codex(-will)?|claude|agy|antigravity|gemini|cursor-agent)(\s|$)")


def _lsof_cwds() -> list[tuple[int, str]]:
    try:
        done = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows: list[tuple[int, str]] = []
    pid: int | None = None
    for line in done.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            rows.append((pid, line[1:]))
    return rows


def _command_of(pid: int) -> str:
    try:
        done = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip()


def kill_provider_clis_in_directory(directory: str | Path) -> list[int]:
    """Terminate provider CLI process groups working inside `directory`, whether or not this process started them.

    For abandoning a task: its worktree is the only handle left on a CLI that outlived the Harness thread that
    was waiting for it. **Enforced narrowing:** only a process group whose *leader* is a provider CLI
    (codex / claude / agy / antigravity / gemini / cursor-agent) and has its working directory inside `directory` is signalled,
    never this process or its group, so a person's own shell sitting in the folder is left alone.
    Returns the group ids signalled."""
    root = os.path.realpath(str(directory))
    own_pgid = os.getpgid(0)
    groups: dict[int, None] = {}
    for pid, cwd in _lsof_cwds():
        real = os.path.realpath(cwd)
        if real != root and not real.startswith(root + os.sep):
            continue
        try:
            pgid = os.getpgid(pid)
        except OSError:
            continue
        if pgid in (0, 1, own_pgid) or pgid in groups:
            continue
        if _PROVIDER_CLI.search(_command_of(pgid)):
            groups[pgid] = None
    for pgid in groups:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
    deadline = monotonic() + 5
    while monotonic() < deadline and any(_group_alive(g) for g in groups):
        time.sleep(0.2)
    for pgid in groups:
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass
    return list(groups)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


class CliProcessRunner:
    """Execute provider CLIs without a shell or provider API client."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_text: str | None = None,
    ) -> ProcessResult:
        started_at = utc_now_iso()
        started = monotonic()
        resolved = resolve_executable(command[0])
        if not resolved:
            return self._unavailable(
                command,
                started_at,
                started,
                f"CLI executable not found: {command[0]}",
            )

        argv = [resolved, *command[1:]]
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env(command),
                # Own process group (POSIX): lets kill_process_for_thread() terminate
                # the whole tree, not just this direct child, and keeps SIGINT/SIGTERM
                # sent to the harness's own process group from also hitting the CLI
                # process directly (we want to control that explicitly, not have it
                # happen implicitly via inherited signal delivery).
                start_new_session=True,
            )
        except (PermissionError, OSError) as exc:
            return self._unavailable(argv, started_at, started, str(exc))

        thread_ident = _register_process(process)
        try:
            try:
                stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self._terminate_process_group(process)
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
                return ProcessResult(
                    command=argv,
                    return_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    started_at=started_at,
                    completed_at=utc_now_iso(),
                    duration_ms=int((monotonic() - started) * 1000),
                    timed_out=True,
                    error=f"CLI timed out after {timeout_seconds} seconds",
                )

            cancelled = _was_cancelled(thread_ident)
            return ProcessResult(
                command=argv,
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                started_at=started_at,
                completed_at=utc_now_iso(),
                duration_ms=int((monotonic() - started) * 1000),
                cancelled=cancelled,
                error="CLI process was cancelled (harness shutdown)" if cancelled else None,
            )
        finally:
            _unregister_process(thread_ident)

    def probe(self, executable: str, *, cwd: Path, timeout_seconds: int = 10) -> ProcessResult:
        return self.run(
            [executable, "--version"], cwd=cwd, timeout_seconds=timeout_seconds
        )

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _unavailable(
        command: list[str], started_at: str, started: float, error: str
    ) -> ProcessResult:
        return ProcessResult(
            command=command,
            return_code=None,
            stdout="",
            stderr="",
            started_at=started_at,
            completed_at=utc_now_iso(),
            duration_ms=int((monotonic() - started) * 1000),
            unavailable=True,
            error=error,
        )


def resolve_executable(executable: str) -> str | None:
    candidate = Path(executable).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        return str(candidate.resolve()) if candidate.exists() else None
    return shutil.which(executable)
