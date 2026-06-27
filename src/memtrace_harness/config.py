from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HarnessConfig:
    memtrace_mcp_url: str | None
    memtrace_api_token: str | None
    trace_db_path: Path

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        trace_path = os.getenv("HARNESS_TRACE_DB", "data/harness.sqlite3")
        return cls(
            memtrace_mcp_url=os.getenv("MEMTRACE_MCP_URL"),
            memtrace_api_token=os.getenv("MEMTRACE_API_TOKEN"),
            trace_db_path=Path(trace_path),
        )
