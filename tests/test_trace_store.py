from contextlib import closing
from datetime import datetime, timezone
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


class ScheduleStoreTests(TestCase):
    def test_create_and_list_schedules_for_project(self) -> None:
        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            next_run = datetime(2026, 9, 18, 1, 0, tzinfo=timezone.utc)
            schedule_id = store.create_schedule(
                project="Beri",
                workspace_id="ws_beri",
                goal="daily backlog review",
                kind="daily",
                interval_seconds=None,
                time_of_day="09:00",
                chat_id=12345,
                next_run_at=next_run,
            )
            self.assertTrue(schedule_id.startswith("sched_"))

            rows = store.list_schedules("Beri")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["id"], schedule_id)
            self.assertEqual(row["project"], "Beri")
            self.assertEqual(row["kind"], "daily")
            self.assertEqual(row["time_of_day"], "09:00")
            self.assertEqual(row["chat_id"], 12345)
            self.assertTrue(row["active"])
            self.assertIsNone(row["last_run_at"])

            # A different project must not see it.
            self.assertEqual(store.list_schedules("OtherProject"), [])

    def test_list_due_schedules_only_returns_active_and_due(self) -> None:
        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            past = datetime(2020, 1, 1, tzinfo=timezone.utc)
            future = datetime(2099, 1, 1, tzinfo=timezone.utc)
            due_id = store.create_schedule(
                project="Beri", workspace_id="ws", goal="g1", kind="interval",
                interval_seconds=3600, next_run_at=past, chat_id=1,
            )
            not_due_id = store.create_schedule(
                project="Beri", workspace_id="ws", goal="g2", kind="interval",
                interval_seconds=3600, next_run_at=future, chat_id=1,
            )
            store.create_schedule(
                project="Beri", workspace_id="ws", goal="g3", kind="interval",
                interval_seconds=3600, next_run_at=past, chat_id=1,
            )
            cancelled_but_due = store.list_schedules("Beri")[-1]["id"]
            store.deactivate_schedule(cancelled_but_due)

            due = store.list_due_schedules(datetime.now(timezone.utc))
            due_ids = {row["id"] for row in due}
            self.assertIn(due_id, due_ids)
            self.assertNotIn(not_due_id, due_ids)
            self.assertNotIn(cancelled_but_due, due_ids)

    def test_deactivate_schedule_is_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            schedule_id = store.create_schedule(
                project="Beri", workspace_id="ws", goal="g", kind="interval",
                interval_seconds=3600, next_run_at=datetime.now(timezone.utc), chat_id=1,
            )
            self.assertTrue(store.deactivate_schedule(schedule_id))
            self.assertFalse(store.deactivate_schedule(schedule_id))
            self.assertFalse(store.deactivate_schedule("sched_doesnotexist"))
            self.assertEqual(store.list_schedules("Beri"), [])
            self.assertEqual(len(store.list_schedules("Beri", active_only=False)), 1)

    def test_mark_schedule_ran_updates_run_fields(self) -> None:
        with TemporaryDirectory() as directory:
            store = TraceStore(Path(directory) / "trace.sqlite3")
            schedule_id = store.create_schedule(
                project="Beri", workspace_id="ws", goal="g", kind="interval",
                interval_seconds=3600, next_run_at=datetime.now(timezone.utc), chat_id=1,
            )
            ran_at = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)
            next_run_at = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
            store.mark_schedule_ran(schedule_id, ran_at=ran_at, next_run_at=next_run_at, status="triggered")

            row = store.get_schedule(schedule_id)
            self.assertEqual(row["last_run_status"], "triggered")
            self.assertEqual(row["next_run_at"], next_run_at.isoformat())
            self.assertEqual(row["last_run_at"], ran_at.isoformat())
