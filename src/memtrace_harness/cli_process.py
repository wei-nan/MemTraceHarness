from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
from time import monotonic


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
# running CLI call from a completely different thread (the gateway's shutdown-drain
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
                env=os.environ.copy(),
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
