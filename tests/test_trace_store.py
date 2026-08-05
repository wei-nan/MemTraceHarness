from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest import TestCase

from memtrace_harness.trace_store import TraceStore


class TraceStoreMigrationTests(TestCase):
    def test_existing_v03_database_gets_additive_conversation_schema(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        workspace_id TEXT NOT NULL,
                        goal TEXT NOT NULL,
                        task_json TEXT NOT NULL,
                        summary_json TEXT,
                        writeback_node_id TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );
                    """
                )
                connection.commit()

            TraceStore(path)

            with closing(sqlite3.connect(path)) as connection:
                run_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(runs)")
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("conversation_id", run_columns)
            self.assertTrue(
                {
                    "conversations",
                    "turns",
                    "provider_sessions",
                    "checkpoints",
                    "memory_refs",
                    "provider_availability",
                }.issubset(tables)
            )

    def test_only_capacity_failures_open_the_quota_circuit(self) -> None:
        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            store.record_provider_availability(
                provider="codex",
                model="gpt-5.6-luna",
                quota_bucket="codex-account",
                failure_category="authentication",
                error_signature="auth",
                cooldown_seconds=3600,
            )
            self.assertTrue(store.quota_bucket_available("codex-account"))

            store.record_provider_availability(
                provider="codex",
                model="gpt-5.6-luna",
                quota_bucket="codex-account",
                failure_category="quota_exhausted",
                error_signature="quota",
                cooldown_seconds=3600,
            )
            self.assertFalse(store.quota_bucket_available("codex-account"))
