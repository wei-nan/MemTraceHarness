from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
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
    error: str | None = None


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
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                input=input_text,
                check=False,
                shell=False,
                env=os.environ.copy(),
            )
            return ProcessResult(
                command=argv,
                return_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                started_at=started_at,
                completed_at=utc_now_iso(),
                duration_ms=int((monotonic() - started) * 1000),
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                command=argv,
                return_code=None,
                stdout=_decode_timeout_stream(exc.stdout),
                stderr=_decode_timeout_stream(exc.stderr),
                started_at=started_at,
                completed_at=utc_now_iso(),
                duration_ms=int((monotonic() - started) * 1000),
                timed_out=True,
                error=f"CLI timed out after {timeout_seconds} seconds",
            )
        except (PermissionError, OSError) as exc:
            return self._unavailable(argv, started_at, started, str(exc))

    def probe(self, executable: str, *, cwd: Path, timeout_seconds: int = 10) -> ProcessResult:
        return self.run(
            [executable, "--version"], cwd=cwd, timeout_seconds=timeout_seconds
        )

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


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
