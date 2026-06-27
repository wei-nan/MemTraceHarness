from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from memtrace_harness.schemas import HarnessSummary, TaskEnvelope, utc_now_iso


class TraceStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create_run(self, task: TaskEnvelope) -> str:
        trace_id = f"run_{uuid4().hex[:12]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (id, workspace_id, goal, task_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    task.workspace_id,
                    task.goal,
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    utc_now_iso(),
                ),
            )
        return trace_id

    def save_summary(self, summary: HarnessSummary) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET summary_json = ?, writeback_node_id = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(summary.to_dict(), ensure_ascii=False),
                    summary.writeback_node_id,
                    utc_now_iso(),
                    summary.trace_id,
                ),
            )
            for response in summary.responses:
                conn.execute(
                    """
                    INSERT INTO model_responses (run_id, adapter_id, role, response_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        summary.trace_id,
                        response.adapter_id,
                        response.role,
                        json.dumps(response.to_dict(), ensure_ascii=False),
                    ),
                )
            for conflict in summary.conflicts:
                conn.execute(
                    """
                    INSERT INTO conflicts (run_id, kind, conflict_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        summary.trace_id,
                        conflict.kind,
                        json.dumps(conflict.to_dict(), ensure_ascii=False),
                    ),
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    summary_json TEXT,
                    writeback_node_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS model_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    conflict_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                """
            )
