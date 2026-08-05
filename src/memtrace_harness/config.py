from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessConfig:
    memtrace_mcp_url: str | None
    memtrace_api_token: str | None
    trace_db_path: Path
    trace_root: Path
    claude_command: str
    codex_command: str
    antigravity_command: str
    antigravity_output_mode: str
    cli_timeout_seconds: int

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        trace_path = os.getenv("HARNESS_TRACE_DB", "data/harness.sqlite3")
        trace_root = os.getenv("HARNESS_TRACE_ROOT", "data/traces")
        return cls(
            memtrace_mcp_url=os.getenv("MEMTRACE_MCP_URL"),
            memtrace_api_token=os.getenv("MEMTRACE_API_TOKEN"),
            trace_db_path=Path(trace_path),
            trace_root=Path(trace_root),
            claude_command=os.getenv("HARNESS_CLAUDE_COMMAND", "claude"),
            codex_command=os.getenv("HARNESS_CODEX_COMMAND", "codex"),
            antigravity_command=os.getenv("HARNESS_ANTIGRAVITY_COMMAND", "agy"),
            antigravity_output_mode=_antigravity_output_mode(
                os.getenv("HARNESS_ANTIGRAVITY_OUTPUT_MODE", "auto")
            ),
            cli_timeout_seconds=_positive_int(os.getenv("HARNESS_CLI_TIMEOUT_SECONDS"), 900),
        )


def _positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("HARNESS_CLI_TIMEOUT_SECONDS must be greater than zero")
    return parsed


def _antigravity_output_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"auto", "text", "stream-json"}:
        raise ValueError(
            "HARNESS_ANTIGRAVITY_OUTPUT_MODE must be auto, text, or stream-json"
        )
    return normalized
