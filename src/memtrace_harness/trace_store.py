from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from memtrace_harness.schemas import (
    HarnessSummary,
    LoopStageResult,
    LoopSummary,
    ModelResponse,
    ResumeEnvelope,
    TaskEnvelope,
    utc_now_iso,
)


class TraceStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create_conversation(
        self, task: TaskEnvelope, conversation_id: str | None = None
    ) -> str:
        conversation_id = conversation_id or f"conv_{uuid4().hex[:12]}"
        now = utc_now_iso()
        with self._connection() as conn:
            existing = conn.execute(
                "SELECT workspace_id, task_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if existing and existing[0] != task.workspace_id:
                raise ValueError(
                    f"Conversation {conversation_id!r} belongs to another workspace"
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO conversations (
                    id, task_id, workspace_id, state, cumulative_tokens,
                    compaction_generation, created_at, updated_at, last_activity_at
                ) VALUES (?, ?, ?, 'active', 0, 0, ?, ?, ?)
                """,
                (conversation_id, task.task_id, task.workspace_id, now, now, now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ?, last_activity_at = ? WHERE id = ?",
                (now, now, conversation_id),
            )
            for ref in task.context_refs:
                workspace_id, node_id = _split_memory_ref(task.workspace_id, ref)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_refs (
                        conversation_id, checkpoint_id, workspace_id, node_id,
                        ref, purpose, created_at
                    ) VALUES (?, NULL, ?, ?, ?, 'task-context', ?)
                    """,
                    (conversation_id, workspace_id, node_id, ref, now),
                )
        return conversation_id

    def conversation_task_id(self, conversation_id: str) -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT task_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        if not row:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        return str(row[0])

    def create_run(
        self, task: TaskEnvelope, *, conversation_id: str | None = None
    ) -> str:
        trace_id = f"run_{uuid4().hex[:12]}"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    id, workspace_id, goal, task_json, conversation_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    task.workspace_id,
                    task.goal,
                    json.dumps(task.to_dict(), ensure_ascii=False),
                    conversation_id,
                    utc_now_iso(),
                ),
            )
            if conversation_id:
                conn.execute(
                    """
                    UPDATE conversations
                    SET active_run_id = ?, updated_at = ?, last_activity_at = ?
                    WHERE id = ?
                    """,
                    (trace_id, utc_now_iso(), utc_now_iso(), conversation_id),
                )
        return trace_id

    def save_summary(self, summary: HarnessSummary) -> None:
        with self._connection() as conn:
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
                self._insert_response(conn, summary.trace_id, response)
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

    def save_loop_summary(self, summary: LoopSummary) -> None:
        with self._connection() as conn:
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
            if summary.conversation_id:
                conn.execute(
                    """
                    UPDATE conversations
                    SET state = ?, active_checkpoint_id = ?,
                        updated_at = ?, last_activity_at = ?
                    WHERE id = ?
                    """,
                    (
                        summary.status,
                        summary.active_checkpoint_id,
                        utc_now_iso(),
                        utc_now_iso(),
                        summary.conversation_id,
                    ),
                )

    def save_stage_result(
        self,
        *,
        conversation_id: str,
        run_id: str,
        result: LoopStageResult,
    ) -> None:
        """Durably append one attempt before another provider may be invoked."""
        now = utc_now_iso()
        execution = result.response.execution
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO loop_stages (
                    run_id, sequence, stage, profile_id, state, artifact_json,
                    attempt_index, fallback_from_model, checkpoint_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.sequence,
                    result.stage,
                    result.profile_id,
                    result.state,
                    json.dumps(result.artifact, ensure_ascii=False)
                    if result.artifact is not None
                    else None,
                    result.attempt_index,
                    result.fallback_from_model,
                    result.checkpoint_id,
                ),
            )
            self._insert_response(
                conn,
                run_id,
                result.response,
                stage=result.stage,
                profile_id=result.profile_id,
                sequence=result.sequence,
                attempt_index=result.attempt_index,
            )
            conn.execute(
                """
                INSERT INTO turns (
                    conversation_id, run_id, sequence, stage, profile_id,
                    attempt_index, provider, model, provider_session_id, state,
                    usage_json, artifact_json, raw_trace_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    run_id,
                    result.sequence,
                    result.stage,
                    result.profile_id,
                    result.attempt_index,
                    execution.provider,
                    execution.resolved_model or execution.requested_model,
                    execution.provider_run_id,
                    result.state,
                    json.dumps(execution.usage.to_dict(), ensure_ascii=False),
                    json.dumps(result.artifact, ensure_ascii=False)
                    if result.artifact is not None
                    else None,
                    execution.raw_trace_ref,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO provider_sessions (
                    conversation_id, run_id, provider, model, profile_id,
                    provider_session_id, status, started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    run_id,
                    execution.provider,
                    execution.resolved_model or execution.requested_model,
                    result.profile_id,
                    execution.provider_run_id,
                    execution.status,
                    execution.started_at or now,
                    execution.completed_at or now,
                ),
            )
            conn.execute(
                """
                UPDATE conversations
                SET cumulative_tokens = cumulative_tokens + ?,
                    updated_at = ?, last_activity_at = ?
                WHERE id = ?
                """,
                (_usage_units(execution.usage), now, now, conversation_id),
            )

    def next_checkpoint_identity(self, conversation_id: str) -> tuple[str, int]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT compaction_generation FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            raise ValueError(f"Unknown conversation: {conversation_id}")
        generation = int(row[0]) + 1
        return f"cp_{uuid4().hex[:12]}", generation

    def save_checkpoint(self, envelope: ResumeEnvelope) -> None:
        payload = json.dumps(envelope.to_dict(), ensure_ascii=False)
        token_before = int(envelope.runtime_context.get("token_spent", 0))
        completeness = str(
            envelope.runtime_context.get("usage_completeness", "unavailable")
        )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    id, conversation_id, run_id, generation, stage, reason,
                    next_action, envelope_json, token_before, token_after,
                    usage_completeness, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    envelope.checkpoint_id,
                    envelope.conversation_id,
                    envelope.run_id,
                    envelope.generation,
                    envelope.current_stage,
                    envelope.reason,
                    envelope.next_action,
                    payload,
                    token_before,
                    completeness,
                    envelope.created_at,
                ),
            )
            default_workspace = str(
                envelope.spec_context.get("workspace_id") or ""
            )
            refs = envelope.spec_context.get("context_refs", [])
            if isinstance(refs, list):
                for ref in refs:
                    if not isinstance(ref, str):
                        continue
                    workspace_id, node_id = _split_memory_ref(default_workspace, ref)
                    conn.execute(
                        """
                        INSERT INTO memory_refs (
                            conversation_id, checkpoint_id, workspace_id, node_id,
                            ref, purpose, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'checkpoint-source', ?)
                        ON CONFLICT(conversation_id, ref, purpose) DO UPDATE SET
                            checkpoint_id = excluded.checkpoint_id,
                            workspace_id = excluded.workspace_id,
                            node_id = excluded.node_id
                        """,
                        (
                            envelope.conversation_id,
                            envelope.checkpoint_id,
                            workspace_id,
                            node_id,
                            ref,
                            envelope.created_at,
                        ),
                    )
            conn.execute(
                """
                UPDATE conversations
                SET active_checkpoint_id = ?, compaction_generation = ?,
                    updated_at = ?, last_activity_at = ?
                WHERE id = ?
                """,
                (
                    envelope.checkpoint_id,
                    envelope.generation,
                    utc_now_iso(),
                    utc_now_iso(),
                    envelope.conversation_id,
                ),
            )

    def latest_resume_envelope(self, conversation_id: str) -> ResumeEnvelope | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT envelope_json FROM checkpoints
                WHERE conversation_id = ?
                ORDER BY generation DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return ResumeEnvelope(**json.loads(row[0]))

    def quota_bucket_available(self, quota_bucket: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT status, cooldown_until FROM provider_availability
                WHERE quota_bucket = ?
                """,
                (quota_bucket,),
            ).fetchone()
        if (
            not row
            or row[0] not in {"quota_exhausted", "rate_limit", "provider_overloaded"}
            or not row[1]
        ):
            return True
        try:
            return datetime.fromisoformat(row[1]) <= datetime.now(timezone.utc)
        except ValueError:
            return False

    def record_provider_availability(
        self,
        *,
        provider: str,
        model: str | None,
        quota_bucket: str,
        failure_category: str,
        error_signature: str | None,
        cooldown_seconds: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        succeeded = failure_category == "none"
        capacity_failure = failure_category in {
            "quota_exhausted",
            "rate_limit",
            "provider_overloaded",
        }
        status = "available" if succeeded else failure_category
        with self._connection() as conn:
            previous = conn.execute(
                "SELECT status, consecutive_failures FROM provider_availability "
                "WHERE quota_bucket = ?",
                (quota_bucket,),
            ).fetchone()
            failures = (
                0
                if succeeded
                else int(previous[1]) + 1
                if previous and previous[0] == failure_category
                else 1
            )
            cooldown_delay = min(
                cooldown_seconds * (2 ** min(max(failures - 1, 0), 8)),
                7 * 24 * 60 * 60,
            )
            cooldown_until = (
                (now + timedelta(seconds=cooldown_delay)).isoformat()
                if capacity_failure
                else None
            )
            conn.execute(
                """
                INSERT INTO provider_availability (
                    quota_bucket, provider, model, status, unavailable_since,
                    cooldown_until, reset_at, reset_at_confidence,
                    consecutive_failures, last_error_signature, last_success_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                ON CONFLICT(quota_bucket) DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    status = excluded.status,
                    unavailable_since = excluded.unavailable_since,
                    cooldown_until = excluded.cooldown_until,
                    consecutive_failures = excluded.consecutive_failures,
                    last_error_signature = excluded.last_error_signature,
                    last_success_at = COALESCE(
                        excluded.last_success_at,
                        provider_availability.last_success_at
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    quota_bucket,
                    provider,
                    model,
                    status,
                    None if succeeded else now.isoformat(),
                    cooldown_until,
                    failures,
                    error_signature,
                    now.isoformat() if succeeded else None,
                    now.isoformat(),
                ),
            )

    def update_run_summary(self, summary: HarnessSummary | LoopSummary) -> None:
        """Update summary/writeback metadata without duplicating execution rows."""
        with self._connection() as conn:
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

    @staticmethod
    def _insert_response(
        conn: sqlite3.Connection,
        run_id: str,
        response: ModelResponse,
        *,
        stage: str | None = None,
        profile_id: str | None = None,
        sequence: int | None = None,
        attempt_index: int = 0,
    ) -> None:
        conn.execute(
            """
            INSERT INTO model_responses (
                run_id, adapter_id, role, response_json, stage, sequence, attempt_index
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                response.adapter_id,
                response.role,
                json.dumps(response.to_dict(), ensure_ascii=False),
                stage,
                sequence,
                attempt_index,
            ),
        )
        execution = response.execution
        conn.execute(
            """
            INSERT INTO cli_executions (
                run_id, adapter_id, provider, status, exit_code, usage_json,
                provider_run_id, raw_trace_ref, stderr_ref, error,
                started_at, completed_at, duration_ms, stage, profile_id,
                requested_model, resolved_model, reasoning_effort, permission,
                context_policy, cli_version, sequence, attempt_index, quota_bucket,
                fallback_index, failure_category, retry_after, reset_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                run_id,
                response.adapter_id,
                execution.provider,
                execution.status,
                execution.exit_code,
                json.dumps(execution.usage.to_dict(), ensure_ascii=False),
                execution.provider_run_id,
                execution.raw_trace_ref,
                execution.stderr_ref,
                execution.error,
                execution.started_at,
                execution.completed_at,
                execution.duration_ms,
                stage,
                profile_id or execution.role_profile_id,
                execution.requested_model,
                execution.resolved_model,
                execution.reasoning_effort,
                execution.permission,
                execution.context_policy,
                execution.cli_version,
                sequence,
                attempt_index,
                execution.quota_bucket,
                execution.fallback_index,
                execution.failure_category,
                execution.retry_after,
                execution.reset_at,
            ),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        # timeout=30: background Agent Loop runs (Telegram gateway, unattended scanner)
        # and the status dashboard's read queries can now all be writing/reading
        # concurrently from separate threads — each opens its own short-lived
        # connection here, but a stage-result write racing a scan-pass write can still
        # briefly contend for SQLite's single-writer lock. 30s is generous headroom
        # over any individual write's actual duration. WAL mode lets read-only queries
        # (e.g. the status dashboard) proceed without waiting on an in-progress writer.
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            self._migrate_workspace_locks(conn)
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    task_json TEXT NOT NULL,
                    summary_json TEXT,
                    writeback_node_id TEXT,
                    conversation_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS model_responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    stage TEXT,
                    sequence INTEGER,
                    attempt_index INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    conflict_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS cli_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exit_code INTEGER,
                    usage_json TEXT NOT NULL,
                    provider_run_id TEXT,
                    raw_trace_ref TEXT,
                    stderr_ref TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    stage TEXT,
                    profile_id TEXT,
                    requested_model TEXT,
                    resolved_model TEXT,
                    reasoning_effort TEXT,
                    permission TEXT,
                    context_policy TEXT,
                    cli_version TEXT,
                    sequence INTEGER,
                    attempt_index INTEGER NOT NULL DEFAULT 0,
                    quota_bucket TEXT,
                    fallback_index INTEGER NOT NULL DEFAULT 0,
                    failure_category TEXT NOT NULL DEFAULT 'none',
                    retry_after TEXT,
                    reset_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS loop_stages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    artifact_json TEXT,
                    attempt_index INTEGER NOT NULL DEFAULT 0,
                    fallback_from_model TEXT,
                    checkpoint_id TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    active_run_id TEXT,
                    state TEXT NOT NULL,
                    cumulative_tokens INTEGER NOT NULL DEFAULT 0,
                    active_checkpoint_id TEXT,
                    compaction_generation INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL DEFAULT 0,
                    provider TEXT NOT NULL,
                    model TEXT,
                    provider_session_id TEXT,
                    state TEXT NOT NULL,
                    usage_json TEXT NOT NULL,
                    artifact_json TEXT,
                    raw_trace_ref TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS provider_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT,
                    profile_id TEXT NOT NULL,
                    provider_session_id TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    next_action TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    token_before INTEGER NOT NULL,
                    token_after INTEGER,
                    usage_completeness TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, generation),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS memory_refs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    checkpoint_id TEXT,
                    workspace_id TEXT,
                    node_id TEXT,
                    ref TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(conversation_id, ref, purpose),
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id)
                );

                CREATE TABLE IF NOT EXISTS provider_availability (
                    quota_bucket TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL,
                    unavailable_since TEXT,
                    cooldown_until TEXT,
                    reset_at TEXT,
                    reset_at_confidence TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error_signature TEXT,
                    last_success_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS primary_sessions_hot_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_session_id TEXT NOT NULL,
                    project TEXT NOT NULL,
                    turn_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    turn_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_work_conversation_id TEXT,
                    consolidated INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    stage_ref TEXT,
                    reason TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    responded_at TEXT,
                    responded_by_chat_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS workspace_locks (
                    conversation_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    locked_at TEXT NOT NULL,
                    schedule_id TEXT
                );

                CREATE TABLE IF NOT EXISTS edit_locks (
                    workspace_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    locked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    conversation_id TEXT,
                    schedule_id TEXT,
                    chat_id INTEGER,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_worktrees (
                    conversation_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    base_branch TEXT,
                    base_commit TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    is_odd_lot INTEGER NOT NULL,
                    price_type TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    estimated_value REAL NOT NULL,
                    risk_json TEXT NOT NULL DEFAULT '{}',
                    confirmation_token TEXT,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    telegram_chat_id INTEGER,
                    telegram_message_id INTEGER,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_order_intents_status
                    ON order_intents(status, project);
                CREATE INDEX IF NOT EXISTS idx_workspace_locks_ws
                    ON workspace_locks(workspace_id);
                CREATE INDEX IF NOT EXISTS idx_task_queue_ws
                    ON task_queue(workspace_id, id);

                CREATE TABLE IF NOT EXISTS telegram_gateway_state (
                    bot_token_hash TEXT PRIMARY KEY,
                    last_offset INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    interval_seconds INTEGER,
                    time_of_day TEXT,
                    end_time_of_day TEXT,
                    chat_id INTEGER,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_run_status TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_digests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    digest_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    provider TEXT,
                    model TEXT,
                    turn_count INTEGER NOT NULL,
                    digest_json TEXT NOT NULL,
                    memtrace_workspace_id TEXT,
                    memtrace_node_id TEXT,
                    UNIQUE(project, digest_date)
                );

                CREATE TABLE IF NOT EXISTS memory_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    first_turn_seq INTEGER NOT NULL,
                    last_turn_seq INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(workspace_id, node_id)
                );

                CREATE TABLE IF NOT EXISTS preference_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    category TEXT NOT NULL,
                    text TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    explicit INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_digest_date TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );

                CREATE TABLE IF NOT EXISTS topic_briefs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    fresh INTEGER NOT NULL DEFAULT 0,
                    related_json TEXT NOT NULL DEFAULT '[]',
                    source_turns_json TEXT NOT NULL DEFAULT '[]',
                    provider TEXT,
                    model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    pushed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS topic_recall_state (
                    project TEXT PRIMARY KEY,
                    pending_count INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_topic_briefs_project
                    ON topic_briefs(project, expires_at);
                CREATE INDEX IF NOT EXISTS idx_turns_conversation
                    ON turns(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_checkpoints_conversation
                    ON checkpoints(conversation_id, generation);
                CREATE INDEX IF NOT EXISTS idx_hot_log_session
                    ON primary_sessions_hot_log(primary_session_id, turn_seq);
                CREATE INDEX IF NOT EXISTS idx_approval_conv
                    ON approval_requests(conversation_id, status);
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(active, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_schedules_project
                    ON schedules(project, active);
                """
            )
            self._ensure_column(conn, "preference_rules", "adopted_by", "TEXT")
            self._ensure_column(conn, "preference_rules", "retire_reason", "TEXT")
            self._ensure_column(conn, "memory_digests", "node_dirty", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "memory_digests", "edges_synced", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "order_intents", "kind", "TEXT NOT NULL DEFAULT 'order'")
            self._ensure_column(conn, "order_intents", "target_order_id", "TEXT")
            self._ensure_column(conn, "primary_sessions_hot_log", "schedule_id", "TEXT")
            self._retag_legacy_schedule_turns(conn)
            self._ensure_column(conn, "schedules", "end_time_of_day", "TEXT")
            self._ensure_column(conn, "schedules", "paused_until", "TEXT")
            self._ensure_column(conn, "approval_requests", "resume_goal", "TEXT")
            self._ensure_column(conn, "approval_requests", "telegram_chat_id", "INTEGER")
            self._ensure_column(conn, "approval_requests", "telegram_message_id", "INTEGER")
            self._ensure_column(conn, "runs", "conversation_id", "TEXT")
            self._ensure_column(conn, "model_responses", "stage", "TEXT")
            self._ensure_column(conn, "model_responses", "sequence", "INTEGER")
            self._ensure_column(
                conn, "model_responses", "attempt_index", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "cli_executions", "stage", "TEXT")
            self._ensure_column(conn, "cli_executions", "profile_id", "TEXT")
            self._ensure_column(conn, "cli_executions", "requested_model", "TEXT")
            self._ensure_column(conn, "cli_executions", "resolved_model", "TEXT")
            self._ensure_column(conn, "cli_executions", "reasoning_effort", "TEXT")
            self._ensure_column(conn, "cli_executions", "permission", "TEXT")
            self._ensure_column(conn, "cli_executions", "context_policy", "TEXT")
            self._ensure_column(conn, "cli_executions", "cli_version", "TEXT")
            self._ensure_column(conn, "cli_executions", "sequence", "INTEGER")
            self._ensure_column(
                conn, "cli_executions", "attempt_index", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "cli_executions", "quota_bucket", "TEXT")
            self._ensure_column(
                conn, "cli_executions", "fallback_index", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                conn,
                "cli_executions",
                "failure_category",
                "TEXT NOT NULL DEFAULT 'none'",
            )
            self._ensure_column(conn, "cli_executions", "retry_after", "TEXT")
            self._ensure_column(conn, "cli_executions", "reset_at", "TEXT")
            self._ensure_column(
                conn, "loop_stages", "attempt_index", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(conn, "loop_stages", "fallback_from_model", "TEXT")
            self._ensure_column(conn, "loop_stages", "checkpoint_id", "TEXT")

    @staticmethod
    def _migrate_workspace_locks(conn: sqlite3.Connection) -> None:
        """workspace_locks used to be keyed by workspace_id (one running task per
        workspace). It is now keyed by conversation_id so a project can run several
        tasks at once; rebuild an old table in place, keeping any lock rows."""
        columns = list(conn.execute("PRAGMA table_info(workspace_locks)"))
        if not columns:
            return
        primary_key = {row[1] for row in columns if row[5]}
        if primary_key == {"conversation_id"}:
            return
        conn.execute("ALTER TABLE workspace_locks RENAME TO workspace_locks_legacy")
        conn.execute(
            """
            CREATE TABLE workspace_locks (
                conversation_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                locked_at TEXT NOT NULL,
                schedule_id TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_locks (conversation_id, workspace_id, locked_at) "
            "SELECT conversation_id, workspace_id, locked_at FROM workspace_locks_legacy"
        )
        conn.execute("DROP TABLE workspace_locks_legacy")

    @staticmethod
    def _retag_legacy_schedule_turns(conn: sqlite3.Connection) -> None:
        """Before 2026-10-02 a schedule firing was logged as an ordinary system
        "decision" turn plus an ordinary "dev_report" turn, so a 10-minute schedule
        flooded the rolling context window with its own noise (54% of one project's
        log). New rows are typed schedule_trigger / schedule_report with a schedule_id;
        this gives the old rows the same shape. Idempotent: only rows still carrying the
        old shape are touched, and no content changes.

        A trigger row is recognised by its exact text. When two schedules fall due
        together only the first gets the workspace lock (the second is refused), so a
        report belongs to the FIRST trigger of the contiguous run of trigger rows right
        before it."""
        triggers = conn.execute(
            "SELECT id, primary_session_id, turn_seq, content FROM primary_sessions_hot_log "
            "WHERE speaker = 'system' AND turn_type = 'decision' AND content LIKE '排程 sched_%觸發：%'"
        ).fetchall()
        for row_id, _session, _seq, content in triggers:
            schedule_id = content.split(" ", 2)[1]
            conn.execute(
                "UPDATE primary_sessions_hot_log SET turn_type = 'schedule_trigger', schedule_id = ? WHERE id = ?",
                (schedule_id, row_id),
            )
        reports = conn.execute(
            "SELECT id, primary_session_id, turn_seq FROM primary_sessions_hot_log "
            "WHERE speaker = 'work_session_report' AND turn_type = 'dev_report' "
            "AND content LIKE 'Chat-triggered loop%' AND schedule_id IS NULL"
        ).fetchall()
        for row_id, session_id, seq in reports:
            first_trigger = None
            cursor_seq = seq - 1
            while cursor_seq >= 1:
                prev = conn.execute(
                    "SELECT turn_type, schedule_id FROM primary_sessions_hot_log "
                    "WHERE primary_session_id = ? AND turn_seq = ?",
                    (session_id, cursor_seq),
                ).fetchone()
                if prev is None or prev[0] != "schedule_trigger" or not prev[1]:
                    break
                first_trigger = prev[1]
                cursor_seq -= 1
            if first_trigger is not None:
                conn.execute(
                    "UPDATE primary_sessions_hot_log SET turn_type = 'schedule_report', schedule_id = ? WHERE id = ?",
                    (first_trigger, row_id),
                )

    def append_primary_session_turn(
        self,
        *,
        primary_session_id: str,
        project: str,
        speaker: str,
        turn_type: str,
        content: str,
        provider: str | None = None,
        model: str | None = None,
        source_work_conversation_id: str | None = None,
        schedule_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_seq), 0) FROM primary_sessions_hot_log WHERE primary_session_id = ?",
                (primary_session_id,),
            ).fetchone()
            next_seq = int(row[0]) + 1 if row else 1
            cursor = conn.execute(
                """
                INSERT INTO primary_sessions_hot_log (
                    primary_session_id, project, turn_seq, created_at, speaker,
                    provider, model, turn_type, content, source_work_conversation_id, consolidated,
                    schedule_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    primary_session_id,
                    project,
                    next_seq,
                    now,
                    speaker,
                    provider,
                    model,
                    turn_type,
                    content,
                    source_work_conversation_id,
                    schedule_id,
                ),
            )
            return cursor.lastrowid

    def get_primary_session_turns(
        self, primary_session_id: str, limit: int | None = None, include_consolidated: bool = True
    ) -> list[dict]:
        with self._connection() as conn:
            query = "SELECT id, primary_session_id, project, turn_seq, created_at, speaker, provider, model, turn_type, content, source_work_conversation_id, consolidated, schedule_id FROM primary_sessions_hot_log WHERE primary_session_id = ?"
            params: list[object] = [primary_session_id]
            if not include_consolidated:
                query += " AND consolidated = 0"
            query += " ORDER BY turn_seq ASC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row[0],
                "primary_session_id": row[1],
                "project": row[2],
                "turn_seq": row[3],
                "created_at": row[4],
                "speaker": row[5],
                "provider": row[6],
                "model": row[7],
                "turn_type": row[8],
                "content": row[9],
                "source_work_conversation_id": row[10],
                "consolidated": bool(row[11]),
                "schedule_id": row[12],
            }
            for row in rows
        ]

    def get_recent_primary_session_turns(self, primary_session_id: str, limit: int) -> list[dict]:
        """Most recent `limit` turns, in chronological order — for display (e.g. the
        status dashboard's conversation preview), not for consolidation bookkeeping."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT id, primary_session_id, project, turn_seq, created_at, speaker, "
                "provider, model, turn_type, content, source_work_conversation_id, consolidated, "
                "schedule_id FROM primary_sessions_hot_log WHERE primary_session_id = ? "
                "ORDER BY turn_seq DESC LIMIT ?",
                (primary_session_id, limit),
            ).fetchall()
        turns = [
            {
                "id": row[0],
                "primary_session_id": row[1],
                "project": row[2],
                "turn_seq": row[3],
                "created_at": row[4],
                "speaker": row[5],
                "provider": row[6],
                "model": row[7],
                "turn_type": row[8],
                "content": row[9],
                "source_work_conversation_id": row[10],
                "consolidated": bool(row[11]),
                "schedule_id": row[12],
            }
            for row in rows
        ]
        turns.reverse()
        return turns

    def get_unconsolidated_turns(self, primary_session_id: str) -> list[dict]:
        return self.get_primary_session_turns(primary_session_id, include_consolidated=False)

    def mark_turns_consolidated(self, turn_ids: list[int]) -> None:
        if not turn_ids:
            return
        with self._connection() as conn:
            placeholders = ",".join("?" * len(turn_ids))
            conn.execute(
                f"UPDATE primary_sessions_hot_log SET consolidated = 1 WHERE id IN ({placeholders})",
                turn_ids,
            )

    # --- Nightly memory digests (memory_digest.py) -------------------------------

    _DIGEST_COLUMNS = (
        "id, project, digest_date, created_at, provider, model, turn_count, digest_json, "
        "memtrace_workspace_id, memtrace_node_id, node_dirty, edges_synced"
    )

    @staticmethod
    def _digest_row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "project": row[1],
            "digest_date": row[2],
            "created_at": row[3],
            "provider": row[4],
            "model": row[5],
            "turn_count": row[6],
            "digest": json.loads(row[7]),
            "memtrace_workspace_id": row[8],
            "memtrace_node_id": row[9],
            "node_dirty": bool(row[10]),
            "edges_synced": bool(row[11]),
        }

    def save_memory_digest(
        self,
        *,
        project: str,
        digest_date: str,
        provider: str | None,
        model: str | None,
        turn_count: int,
        digest: dict,
    ) -> int:
        """Insert or replace that project's digest for that local date. Replacing keeps
        the MemTrace node it already has and marks it dirty, so the next sync updates
        that node in place instead of leaving the old version next to a new one."""
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_digests (
                    project, digest_date, created_at, provider, model, turn_count, digest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project, digest_date) DO UPDATE SET
                    created_at = excluded.created_at,
                    provider = excluded.provider,
                    model = excluded.model,
                    turn_count = excluded.turn_count,
                    digest_json = excluded.digest_json,
                    node_dirty = 1
                """,
                (
                    project,
                    digest_date,
                    utc_now_iso(),
                    provider,
                    model,
                    turn_count,
                    json.dumps(digest, ensure_ascii=False),
                ),
            )
            row = conn.execute(
                "SELECT id FROM memory_digests WHERE project = ? AND digest_date = ?",
                (project, digest_date),
            ).fetchone()
        return int(row[0]) if row else int(cur.lastrowid or 0)

    # --- topic recall: short-lived topic briefs (topic_recall.py) -----------------------

    _BRIEF_COLUMNS = (
        "id, project, title, summary, fresh, related_json, source_turns_json, provider, "
        "model, created_at, updated_at, expires_at, pushed_at"
    )

    @staticmethod
    def _brief_row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "project": row[1],
            "title": row[2],
            "summary": row[3],
            "fresh": bool(row[4]),
            "related": json.loads(row[5]),
            "source_turns": json.loads(row[6]),
            "provider": row[7],
            "model": row[8],
            "created_at": row[9],
            "updated_at": row[10],
            "expires_at": row[11],
            "pushed_at": row[12],
        }

    def insert_topic_brief(
        self,
        *,
        project: str,
        title: str,
        summary: str,
        fresh: bool,
        related: list[dict],
        source_turns: list[int],
        provider: str | None,
        model: str | None,
        expires_at: str,
    ) -> int:
        now = utc_now_iso()
        with self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO topic_briefs (project, title, summary, fresh, related_json, "
                "source_turns_json, provider, model, created_at, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project, title, summary, int(fresh),
                    json.dumps(related, ensure_ascii=False),
                    json.dumps(source_turns), provider, model, now, now, expires_at,
                ),
            )
        return int(cur.lastrowid or 0)

    def update_topic_brief(
        self,
        brief_id: int,
        *,
        summary: str,
        related: list[dict],
        source_turns: list[int],
        provider: str | None,
        model: str | None,
        expires_at: str,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE topic_briefs SET summary = ?, related_json = ?, source_turns_json = ?, "
                "provider = ?, model = ?, updated_at = ?, expires_at = ?, "
                "fresh = CASE WHEN ? = '[]' THEN fresh ELSE 0 END WHERE id = ?",
                (
                    summary, json.dumps(related, ensure_ascii=False), json.dumps(source_turns),
                    provider, model, utc_now_iso(), expires_at,
                    json.dumps(related, ensure_ascii=False), brief_id,
                ),
            )

    def extend_topic_brief(self, brief_id: int, expires_at: str) -> None:
        """The topic came up again: keep the brief alive without changing its content."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE topic_briefs SET expires_at = ?, updated_at = ? WHERE id = ?",
                (expires_at, utc_now_iso(), brief_id),
            )

    def mark_topic_brief_pushed(self, brief_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE topic_briefs SET pushed_at = ? WHERE id = ?", (utc_now_iso(), brief_id)
            )

    def list_active_topic_briefs(self, project: str, now_iso: str) -> list[dict]:
        """Briefs not yet expired, most recently touched first."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {self._BRIEF_COLUMNS} FROM topic_briefs WHERE project = ? AND "
                "expires_at > ? ORDER BY updated_at DESC, id DESC",
                (project, now_iso),
            ).fetchall()
        return [self._brief_row_to_dict(r) for r in rows]

    def add_recall_pending(self, project: str, delta: int) -> int:
        """Count of recall runs queued or running for the project (clamped at 0)."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO topic_recall_state (project, pending_count) VALUES (?, MAX(0, ?)) "
                "ON CONFLICT(project) DO UPDATE SET pending_count = MAX(0, pending_count + ?)",
                (project, delta, delta),
            )
            row = conn.execute(
                "SELECT pending_count FROM topic_recall_state WHERE project = ?", (project,)
            ).fetchone()
        return int(row[0])

    def finish_recall_run(self, project: str, error: str | None) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO topic_recall_state (project, pending_count, last_run_at, last_error) "
                "VALUES (?, 0, ?, ?) ON CONFLICT(project) DO UPDATE SET "
                "last_run_at = excluded.last_run_at, last_error = excluded.last_error",
                (project, utc_now_iso(), error),
            )

    def get_recall_state(self, project: str) -> dict:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT pending_count, last_run_at, last_error FROM topic_recall_state "
                "WHERE project = ?",
                (project,),
            ).fetchone()
        return (
            {"pending": int(row[0]), "last_run_at": row[1], "last_error": row[2]}
            if row
            else {"pending": 0, "last_run_at": None, "last_error": None}
        )

    def get_memory_digest(self, project: str, digest_date: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._DIGEST_COLUMNS} FROM memory_digests "
                "WHERE project = ? AND digest_date = ?",
                (project, digest_date),
            ).fetchone()
        return self._digest_row_to_dict(row) if row else None

    def list_memory_digests(self, project: str, limit: int | None = None) -> list[dict]:
        """Newest first."""
        query = (
            f"SELECT {self._DIGEST_COLUMNS} FROM memory_digests WHERE project = ? "
            "ORDER BY digest_date DESC"
        )
        params: tuple = (project,)
        if limit is not None:
            query += " LIMIT ?"
            params = (project, limit)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._digest_row_to_dict(row) for row in rows]

    def list_digests_needing_sync(self, project: str, workspace_id: str) -> list[dict]:
        """Digests never written to MemTrace, written to a different workspace than the
        project's current memory workspace (e.g. it just got a dedicated one), or
        regenerated since they were written."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {self._DIGEST_COLUMNS} FROM memory_digests WHERE project = ? AND "
                "(memtrace_node_id IS NULL OR memtrace_workspace_id IS NOT ? OR node_dirty = 1) "
                "ORDER BY digest_date ASC",
                (project, workspace_id),
            ).fetchall()
        return [self._digest_row_to_dict(row) for row in rows]

    def mark_digest_synced(
        self, digest_id: int, workspace_id: str, node_id: str, *, new_node: bool = True
    ) -> None:
        """new_node=True: a node was just created, so its edges still have to be made.
        False: an existing node was updated in place and keeps the edges it has."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE memory_digests SET memtrace_workspace_id = ?, memtrace_node_id = ?, "
                "node_dirty = 0, edges_synced = CASE WHEN ? THEN 0 ELSE edges_synced END "
                "WHERE id = ?",
                (workspace_id, node_id, 1 if new_node else 0, digest_id),
            )

    def list_digests_needing_edges(self, project: str, workspace_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {self._DIGEST_COLUMNS} FROM memory_digests WHERE project = ? AND "
                "memtrace_workspace_id = ? AND memtrace_node_id IS NOT NULL AND edges_synced = 0 "
                "ORDER BY digest_date ASC",
                (project, workspace_id),
            ).fetchall()
        return [self._digest_row_to_dict(row) for row in rows]

    def mark_digest_edges_synced(self, digest_id: int) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE memory_digests SET edges_synced = 1 WHERE id = ?", (digest_id,))

    def previous_synced_digest(self, project: str, workspace_id: str, before_date: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._DIGEST_COLUMNS} FROM memory_digests WHERE project = ? AND "
                "memtrace_workspace_id = ? AND memtrace_node_id IS NOT NULL AND digest_date < ? "
                "ORDER BY digest_date DESC LIMIT 1",
                (project, workspace_id, before_date),
            ).fetchone()
        return self._digest_row_to_dict(row) if row else None

    # --- Hourly archive nodes written to MemTrace (memory_drafts) ------------------

    def record_memory_draft(
        self, *, project: str, workspace_id: str, node_id: str, first_turn_seq: int, last_turn_seq: int
    ) -> None:
        """Remember which MemTrace node holds which turns. Until 2026-10-02 the id
        create_node returned was thrown away, so nothing could be linked to a draft."""
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO memory_drafts (
                    project, workspace_id, node_id, first_turn_seq, last_turn_seq, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, node_id) DO UPDATE SET
                    first_turn_seq = excluded.first_turn_seq,
                    last_turn_seq = excluded.last_turn_seq
                """,
                (project, workspace_id, node_id, first_turn_seq, last_turn_seq, utc_now_iso()),
            )

    def list_memory_drafts(self, project: str, workspace_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT node_id, first_turn_seq, last_turn_seq, created_at FROM memory_drafts "
                "WHERE project = ? AND workspace_id = ? ORDER BY first_turn_seq ASC",
                (project, workspace_id),
            ).fetchall()
        return [
            {"node_id": r[0], "first_turn_seq": r[1], "last_turn_seq": r[2], "created_at": r[3]}
            for r in rows
        ]

    # --- Operator preference rules (candidates -> human review -> adopted) ---------

    _PREFERENCE_COLUMNS = (
        "id, project, scope, category, text, evidence_json, explicit, status, "
        "source_digest_date, created_at, resolved_at, adopted_by, retire_reason"
    )

    @staticmethod
    def _preference_row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "project": row[1],
            "scope": row[2],
            "category": row[3],
            "text": row[4],
            "evidence": json.loads(row[5]),
            "explicit": bool(row[6]),
            "status": row[7],
            "source_digest_date": row[8],
            "created_at": row[9],
            "resolved_at": row[10],
            "adopted_by": row[11],
            "retire_reason": row[12],
        }

    def add_preference_candidate(
        self,
        *,
        project: str,
        scope: str,
        category: str,
        text: str,
        evidence: list[dict],
        explicit: bool,
        source_digest_date: str | None,
        status: str = "pending",
        adopted_by: str | None = None,
    ) -> int:
        """status="adopted" is how the Harness adopts a preference on its own (nightly
        digest, or a correction made in chat); adopted_by then says who did."""
        if status not in ("pending", "adopted"):
            raise ValueError(f"a new preference starts pending or adopted, not {status!r}")
        now = utc_now_iso()
        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO preference_rules (
                    project, scope, category, text, evidence_json, explicit, status,
                    source_digest_date, created_at, resolved_at, adopted_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project,
                    scope,
                    category,
                    text,
                    json.dumps(evidence, ensure_ascii=False),
                    1 if explicit else 0,
                    status,
                    source_digest_date,
                    now,
                    now if status == "adopted" else None,
                    adopted_by if status == "adopted" else None,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_preference_rules(
        self, *, statuses: tuple[str, ...] | None = None, project: str | None = None
    ) -> list[dict]:
        query = f"SELECT {self._PREFERENCE_COLUMNS} FROM preference_rules WHERE 1 = 1"
        params: list = []
        if statuses:
            query += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if project is not None:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id ASC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._preference_row_to_dict(row) for row in rows]

    def get_preference_rule(self, rule_id: int) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._PREFERENCE_COLUMNS} FROM preference_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return self._preference_row_to_dict(row) if row else None

    def update_preference_rule(
        self,
        rule_id: int,
        *,
        status: str,
        expected_statuses: tuple[str, ...],
        text: str | None = None,
        scope: str | None = None,
        evidence: list[dict] | None = None,
        retire_reason: str | None = None,
        adopted_by: str | None = None,
    ) -> bool:
        """Compare-and-set on status so a double click (or the web page and a CLI at
        once) can't resolve the same rule twice."""
        sets = ["status = ?", "resolved_at = ?"]
        params: list = [status, utc_now_iso()]
        if text is not None:
            sets.append("text = ?")
            params.append(text)
        if scope is not None:
            sets.append("scope = ?")
            params.append(scope)
        if evidence is not None:
            sets.append("evidence_json = ?")
            params.append(json.dumps(evidence, ensure_ascii=False))
        if retire_reason is not None:
            sets.append("retire_reason = ?")
            params.append(retire_reason)
        if adopted_by is not None:
            sets.append("adopted_by = ?")
            params.append(adopted_by)
        params.append(rule_id)
        params.extend(expected_statuses)
        with self._connection() as conn:
            cur = conn.execute(
                f"UPDATE preference_rules SET {', '.join(sets)} WHERE id = ? "
                f"AND status IN ({','.join('?' * len(expected_statuses))})",
                params,
            )
            return cur.rowcount > 0

    def create_approval_request(
        self,
        *,
        conversation_id: str,
        workspace: str,
        working_directory: str,
        reason: str,
        proposed_action: str,
        stage_ref: str | None = None,
        resume_goal: str | None = None,
    ) -> str:
        request_id = f"appr_{uuid4().hex[:12]}"
        now = utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO approval_requests (
                    id, conversation_id, workspace, working_directory, stage_ref,
                    reason, proposed_action, status, created_at, resume_goal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    request_id,
                    conversation_id,
                    workspace,
                    working_directory,
                    stage_ref,
                    reason,
                    proposed_action,
                    now,
                    resume_goal,
                ),
            )
        return request_id

    def count_approval_requests(self, conversation_id: str, reasons: set[str]) -> int:
        """How many approval requests this conversation has already raised for any of
        `reasons` (any status) — used to notice a task that keeps stopping on the same
        kind of question."""
        if not reasons:
            return 0
        placeholders = ",".join("?" for _ in reasons)
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM approval_requests "
                f"WHERE conversation_id = ? AND reason IN ({placeholders})",
                (conversation_id, *sorted(reasons)),
            ).fetchone()
        return int(row[0])

    def list_stale_pending_approvals(self, workspaces: set[str], older_than_iso: str) -> list[dict]:
        """Pending approval requests for `workspaces` created before `older_than_iso`."""
        if not workspaces:
            return []
        placeholders = ",".join("?" for _ in workspaces)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT id FROM approval_requests WHERE status = 'pending' "
                f"AND created_at < ? AND workspace IN ({placeholders}) ORDER BY created_at",
                (older_than_iso, *sorted(workspaces)),
            ).fetchall()
        return [d for d in (self.get_approval_request(r[0]) for r in rows) if d]

    def get_approval_request(self, request_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, workspace, working_directory, stage_ref,
                       reason, proposed_action, status, created_at, responded_at, responded_by_chat_id,
                       resume_goal, telegram_chat_id, telegram_message_id
                FROM approval_requests WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "conversation_id": row[1],
            "workspace": row[2],
            "working_directory": row[3],
            "stage_ref": row[4],
            "reason": row[5],
            "proposed_action": row[6],
            "status": row[7],
            "created_at": row[8],
            "responded_at": row[9],
            "responded_by_chat_id": row[10],
            "resume_goal": row[11],
            "telegram_chat_id": row[12],
            "telegram_message_id": row[13],
        }

    def get_pending_approval_request(self, conversation_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, workspace, working_directory, stage_ref,
                       reason, proposed_action, status, created_at, responded_at, responded_by_chat_id,
                       resume_goal, telegram_chat_id, telegram_message_id
                FROM approval_requests WHERE conversation_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "conversation_id": row[1],
            "workspace": row[2],
            "working_directory": row[3],
            "stage_ref": row[4],
            "reason": row[5],
            "proposed_action": row[6],
            "status": row[7],
            "created_at": row[8],
            "responded_at": row[9],
            "responded_by_chat_id": row[10],
            "resume_goal": row[11],
            "telegram_chat_id": row[12],
            "telegram_message_id": row[13],
        }

    def list_pending_approvals_for_workspace(self, workspace_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, conversation_id, workspace, working_directory, stage_ref,
                       reason, proposed_action, status, created_at, responded_at, responded_by_chat_id,
                       resume_goal, telegram_chat_id, telegram_message_id
                FROM approval_requests WHERE workspace = ? AND status = 'pending'
                ORDER BY created_at ASC
                """,
                (workspace_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "conversation_id": row[1],
                "workspace": row[2],
                "working_directory": row[3],
                "stage_ref": row[4],
                "reason": row[5],
                "proposed_action": row[6],
                "status": row[7],
                "created_at": row[8],
                "responded_at": row[9],
                "responded_by_chat_id": row[10],
                "resume_goal": row[11],
                "telegram_chat_id": row[12],
                "telegram_message_id": row[13],
            }
            for row in rows
        ]

    def list_recently_declined_stage_refs(self, workspace_id: str, reason: str) -> set[str]:
        """stage_ref values of rejected approval_requests for this workspace+reason —
        lets the unattended scanner skip a Task Node the operator already said "not
        this one" to and propose the next one in queue order instead, rather than
        re-proposing the same declined node on every subsequent scan pass."""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT stage_ref FROM approval_requests
                WHERE workspace = ? AND reason = ? AND status = 'rejected' AND stage_ref IS NOT NULL
                """,
                (workspace_id, reason),
            ).fetchall()
        return {row[0] for row in rows}

    def set_approval_telegram_message(
        self, request_id: str, *, chat_id: int, message_id: int
    ) -> None:
        """Records which sent Telegram message carries this approval request, so a
        native swipe-reply to it can be resolved back to the exact approval without
        the human typing an ID — see get_approval_request_by_message_id()."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE approval_requests SET telegram_chat_id = ?, telegram_message_id = ? WHERE id = ?",
                (chat_id, message_id, request_id),
            )

    def get_approval_request_by_message_id(self, chat_id: int, message_id: int) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, conversation_id, workspace, working_directory, stage_ref,
                       reason, proposed_action, status, created_at, responded_at, responded_by_chat_id,
                       resume_goal, telegram_chat_id, telegram_message_id
                FROM approval_requests WHERE telegram_chat_id = ? AND telegram_message_id = ?
                """,
                (chat_id, message_id),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "conversation_id": row[1],
            "workspace": row[2],
            "working_directory": row[3],
            "stage_ref": row[4],
            "reason": row[5],
            "proposed_action": row[6],
            "status": row[7],
            "created_at": row[8],
            "responded_at": row[9],
            "responded_by_chat_id": row[10],
            "resume_goal": row[11],
            "telegram_chat_id": row[12],
            "telegram_message_id": row[13],
        }

    def resolve_approval_request(
        self, request_id: str, status: str, responded_by_chat_id: int | None = None
    ) -> bool:
        if status not in {"approved", "rejected", "expired"}:
            raise ValueError(f"Invalid approval status: {status}")
        now = utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, responded_at = ?, responded_by_chat_id = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, now, responded_by_chat_id, request_id),
            )
            return cursor.rowcount > 0

    def acquire_task_slot(
        self,
        workspace_id: str,
        conversation_id: str,
        *,
        max_slots: int = 1,
        schedule_id: str | None = None,
        check_queue: bool = True,
    ) -> str:
        """Claim one of a workspace's `max_slots` concurrent-task slots, atomically.

        Returns "acquired", "already_running" (this conversation already holds a slot),
        "full" (every slot is taken — the caller may queue), or
        "duplicate_schedule" (this schedule's previous run is still running, or is
        still waiting in the queue — the caller must skip this occurrence rather than
        queue it, so a slow schedule never piles up behind itself). `check_queue=False`
        is for the queue drain, which is starting the very item that sits in the queue."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if schedule_id:
                running = conn.execute(
                    "SELECT 1 FROM workspace_locks WHERE schedule_id = ? LIMIT 1",
                    (schedule_id,),
                ).fetchone()
                if running:
                    return "duplicate_schedule"
                if check_queue and conn.execute(
                    "SELECT 1 FROM task_queue WHERE schedule_id = ? LIMIT 1", (schedule_id,)
                ).fetchone():
                    return "duplicate_schedule"
            taken = conn.execute(
                "SELECT COUNT(*) FROM workspace_locks WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()[0]
            if taken >= max(1, max_slots):
                return "full"
            try:
                conn.execute(
                    "INSERT INTO workspace_locks (conversation_id, workspace_id, locked_at, schedule_id) "
                    "VALUES (?, ?, ?, ?)",
                    (conversation_id, workspace_id, utc_now_iso(), schedule_id),
                )
            except sqlite3.IntegrityError:
                # This very conversation already holds a slot (a double-tapped approval).
                return "already_running"
            return "acquired"

    def acquire_workspace_lock(self, workspace_id: str, conversation_id: str) -> bool:
        """Exclusive claim of a whole workspace (one slot) — used by the unattended
        scanner, which proposes one task at a time."""
        return (
            self.acquire_task_slot(workspace_id, conversation_id, max_slots=1) == "acquired"
        )

    def release_workspace_lock(
        self, workspace_id: str, conversation_id: str | None = None
    ) -> None:
        """Free one task's slot, or — with no conversation_id — every slot of the
        workspace (shutdown and operator recovery, where the owner is unknown). A
        development lock held by the released task goes with it."""
        with self._connection() as conn:
            if conversation_id is None:
                conn.execute("DELETE FROM workspace_locks WHERE workspace_id = ?", (workspace_id,))
                conn.execute("DELETE FROM edit_locks WHERE workspace_id = ?", (workspace_id,))
            else:
                conn.execute(
                    "DELETE FROM workspace_locks WHERE workspace_id = ? AND conversation_id = ?",
                    (workspace_id, conversation_id),
                )
                conn.execute(
                    "DELETE FROM edit_locks WHERE workspace_id = ? AND conversation_id = ?",
                    (workspace_id, conversation_id),
                )

    def acquire_edit_lock(self, workspace_id: str, conversation_id: str) -> bool:
        """The right to change a project's files in place. A project that cannot give each
        task its own worktree lets any number of tasks run side by side (checking, querying,
        running scripts), but only one at a time may *develop*: this lock is that turn.
        Re-entrant for the same conversation. A lock whose owner no longer holds a slot
        (its process was killed) is stale and is taken over."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM edit_locks WHERE workspace_id = ? AND conversation_id NOT IN "
                "(SELECT conversation_id FROM workspace_locks)",
                (workspace_id,),
            )
            holder = conn.execute(
                "SELECT conversation_id FROM edit_locks WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if holder is not None:
                return holder[0] == conversation_id
            conn.execute(
                "INSERT INTO edit_locks (workspace_id, conversation_id, locked_at) VALUES (?, ?, ?)",
                (workspace_id, conversation_id, utc_now_iso()),
            )
            return True

    def release_edit_lock(self, workspace_id: str, conversation_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM edit_locks WHERE workspace_id = ? AND conversation_id = ?",
                (workspace_id, conversation_id),
            )

    def get_edit_lock(self, workspace_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT conversation_id, locked_at FROM edit_locks WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return {"conversation_id": row[0], "locked_at": row[1]} if row else None

    def list_workspace_locks(self, workspace_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT workspace_id, conversation_id, locked_at, schedule_id FROM workspace_locks "
                "WHERE workspace_id = ? ORDER BY locked_at, conversation_id",
                (workspace_id,),
            ).fetchall()
        return [
            {"workspace_id": r[0], "conversation_id": r[1], "locked_at": r[2], "schedule_id": r[3]}
            for r in rows
        ]

    def schedule_in_flight(self, schedule_id: str) -> bool:
        """True while this schedule has a task running or waiting in the queue."""
        with self._connection() as conn:
            return bool(
                conn.execute(
                    "SELECT 1 FROM workspace_locks WHERE schedule_id = ? "
                    "UNION ALL SELECT 1 FROM task_queue WHERE schedule_id = ? LIMIT 1",
                    (schedule_id, schedule_id),
                ).fetchone()
            )

    def enqueue_task(
        self,
        *,
        workspace_id: str,
        kind: str,
        payload: dict,
        conversation_id: str | None = None,
        schedule_id: str | None = None,
        chat_id: int | None = None,
    ) -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO task_queue (workspace_id, kind, conversation_id, schedule_id, "
                "chat_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace_id, kind, conversation_id, schedule_id, chat_id,
                    json.dumps(payload, ensure_ascii=False), utc_now_iso(),
                ),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _queue_row_to_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "workspace_id": row[1],
            "kind": row[2],
            "conversation_id": row[3],
            "schedule_id": row[4],
            "chat_id": row[5],
            "payload": json.loads(row[6]),
            "created_at": row[7],
        }

    _QUEUE_COLUMNS = (
        "id, workspace_id, kind, conversation_id, schedule_id, chat_id, payload_json, created_at"
    )

    def peek_next_queued_task(self, workspace_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._QUEUE_COLUMNS} FROM task_queue WHERE workspace_id = ? "
                "ORDER BY id LIMIT 1",
                (workspace_id,),
            ).fetchone()
        return self._queue_row_to_dict(row) if row else None

    def claim_queued_task(self, queue_id: int) -> bool:
        """Remove a queue entry; False when another drainer already took it."""
        with self._connection() as conn:
            return conn.execute("DELETE FROM task_queue WHERE id = ?", (queue_id,)).rowcount > 0

    def list_queued_tasks(self, workspace_id: str) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {self._QUEUE_COLUMNS} FROM task_queue WHERE workspace_id = ? ORDER BY id",
                (workspace_id,),
            ).fetchall()
        return [self._queue_row_to_dict(r) for r in rows]

    def queued_workspace_ids(self) -> list[str]:
        with self._connection() as conn:
            return [
                r[0] for r in conn.execute("SELECT DISTINCT workspace_id FROM task_queue")
            ]

    def record_conversation_worktree(
        self,
        conversation_id: str,
        *,
        workspace_id: str,
        path: str,
        branch: str,
        base_branch: str | None,
        base_commit: str | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO conversation_worktrees (conversation_id, workspace_id, "
                "path, branch, base_branch, base_commit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, workspace_id, path, branch, base_branch, base_commit, utc_now_iso()),
            )

    def get_conversation_worktree(self, conversation_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT conversation_id, workspace_id, path, branch, base_branch, base_commit "
                "FROM conversation_worktrees WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "conversation_id": row[0], "workspace_id": row[1], "path": row[2],
            "branch": row[3], "base_branch": row[4], "base_commit": row[5],
        }

    def delete_conversation_worktree(self, conversation_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM conversation_worktrees WHERE conversation_id = ?", (conversation_id,)
            )

    # ---- order intents: a trade the model proposed, waiting for the human's button.
    # Status flow: pending -> awaiting (shown in Telegram) -> confirming -> submitted|failed,
    # or awaiting -> cancelled|expired. Every move is a compare-and-set so a double tap or a
    # second gateway can never send the same order twice.

    _ORDER_COLUMNS = (
        "intent_id, project, symbol, action, price, quantity, is_odd_lot, price_type, "
        "order_type, estimated_value, risk_json, confirmation_token, status, expires_at, "
        "telegram_chat_id, telegram_message_id, result_json, created_at, kind, target_order_id"
    )

    @staticmethod
    def _order_row_to_dict(row: tuple) -> dict:
        return {
            "intent_id": row[0], "project": row[1], "symbol": row[2], "action": row[3],
            "price": row[4], "quantity": row[5], "is_odd_lot": bool(row[6]),
            "price_type": row[7], "order_type": row[8], "estimated_value": row[9],
            "risk": json.loads(row[10] or "{}"), "confirmation_token": row[11],
            "status": row[12], "expires_at": row[13], "telegram_chat_id": row[14],
            "telegram_message_id": row[15],
            "result": json.loads(row[16]) if row[16] else None, "created_at": row[17],
            "kind": row[18], "target_order_id": row[19],
        }

    def create_order_intent(
        self,
        *,
        intent_id: str,
        project: str,
        symbol: str,
        action: str,
        price: float,
        quantity: int,
        is_odd_lot: bool,
        price_type: str,
        order_type: str,
        estimated_value: float,
        risk: dict,
        confirmation_token: str,
        expires_at: str,
    ) -> bool:
        """False when this intent is already recorded (a retried proposal)."""
        now = utc_now_iso()
        with self._connection() as conn:
            return conn.execute(
                "INSERT OR IGNORE INTO order_intents (intent_id, project, symbol, action, price, "
                "quantity, is_odd_lot, price_type, order_type, estimated_value, risk_json, "
                "confirmation_token, status, expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    intent_id, project, symbol, action, price, quantity, int(is_odd_lot),
                    price_type, order_type, estimated_value,
                    json.dumps(risk, ensure_ascii=False), confirmation_token, expires_at, now, now,
                ),
            ).rowcount > 0

    def create_cancel_request(
        self,
        *,
        intent_id: str,
        project: str,
        target_order_id: str,
        symbol: str,
        action: str,
        price: float,
        quantity: int,
        is_odd_lot: bool,
        expires_at: str,
    ) -> None:
        """A proposal to cancel an existing order. Same life cycle as a new-order proposal
        (shown with buttons, sent only on the human's tap), but there is no token: the
        cancel is a DELETE the gateway makes itself."""
        now = utc_now_iso()
        estimated = round(price * quantity * (1 if is_odd_lot else 1000), 2)
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO order_intents (intent_id, project, symbol, action, price, "
                "quantity, is_odd_lot, price_type, order_type, estimated_value, risk_json, "
                "confirmation_token, status, expires_at, created_at, updated_at, kind, "
                "target_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'LMT', 'ROD', ?, '{}', NULL, "
                "'pending', ?, ?, ?, 'cancel', ?)",
                (intent_id, project, symbol, action, price, quantity, int(is_odd_lot),
                 estimated, expires_at, now, now, target_order_id),
            )

    def find_open_cancel_request(self, target_order_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._ORDER_COLUMNS} FROM order_intents WHERE kind = 'cancel' "
                "AND target_order_id = ? AND status IN ('pending', 'awaiting', 'confirming') "
                "ORDER BY created_at DESC LIMIT 1",
                (target_order_id,),
            ).fetchone()
        return self._order_row_to_dict(row) if row else None

    def get_order_intent(self, intent_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._ORDER_COLUMNS} FROM order_intents WHERE intent_id = ?", (intent_id,)
            ).fetchone()
        return self._order_row_to_dict(row) if row else None

    def transition_order_intent(
        self, intent_id: str, from_statuses: tuple[str, ...], to_status: str
    ) -> bool:
        marks = ",".join("?" for _ in from_statuses)
        with self._connection() as conn:
            return conn.execute(
                f"UPDATE order_intents SET status = ?, updated_at = ? "
                f"WHERE intent_id = ? AND status IN ({marks})",
                (to_status, utc_now_iso(), intent_id, *from_statuses),
            ).rowcount > 0

    def claim_pending_order_intents(self, projects: list[str]) -> list[dict]:
        """Take the proposals no gateway has shown yet for these projects."""
        if not projects:
            return []
        marks = ",".join("?" for _ in projects)
        with self._connection() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT intent_id FROM order_intents WHERE status = 'pending' "
                    f"AND project IN ({marks}) ORDER BY created_at",
                    projects,
                )
            ]
        claimed = [i for i in ids if self.transition_order_intent(i, ("pending",), "awaiting")]
        return [d for d in (self.get_order_intent(i) for i in claimed) if d]

    def reshow_order_intent(self, intent_id: str) -> bool:
        """The model proposed the same still-pending order again: have the gateway send the
        confirm buttons once more (at the bottom of the chat, where the human is looking)."""
        return self.transition_order_intent(intent_id, ("awaiting",), "pending")

    def set_order_intent_message(self, intent_id: str, *, chat_id: int, message_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE order_intents SET telegram_chat_id = ?, telegram_message_id = ? "
                "WHERE intent_id = ?",
                (chat_id, message_id, intent_id),
            )

    def finish_order_intent(self, intent_id: str, status: str, result: dict | None = None) -> None:
        """Record the outcome and drop the one-time token: it is useless afterwards and
        shouldn't sit in the database."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE order_intents SET status = ?, result_json = ?, confirmation_token = NULL, "
                "updated_at = ? WHERE intent_id = ?",
                (status, json.dumps(result, ensure_ascii=False) if result else None,
                 utc_now_iso(), intent_id),
            )

    def expire_order_intents(self, now_iso: str) -> list[dict]:
        """Proposals nobody confirmed in time. Returned so the gateway can tell the human."""
        with self._connection() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT intent_id FROM order_intents WHERE status IN ('pending', 'awaiting') "
                    "AND expires_at <= ?",
                    (now_iso,),
                )
            ]
        expired = [i for i in ids if self.transition_order_intent(i, ("pending", "awaiting"), "expired")]
        rows = [d for d in (self.get_order_intent(i) for i in expired) if d]
        for row in rows:
            self.finish_order_intent(row["intent_id"], "expired")
        return rows

    def get_latest_turn(self, conversation_id: str) -> dict | None:
        """Most recently completed Agent Loop stage for a conversation — written by
        save_stage_result() as each stage finishes, so this reflects live progress
        (e.g. "planner just finished, red-team is running now") for a run still in
        flight, not only a run's final summary. Used by the status dashboard to show
        what a locked workspace is actually doing right now."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT stage, profile_id, provider, model, state, created_at "
                "FROM turns WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "stage": row[0],
            "profile_id": row[1],
            "provider": row[2],
            "model": row[3],
            "state": row[4],
            "created_at": row[5],
        }

    def get_conversation_pipeline(self, conversation_id: str) -> list[dict]:
        """Every stage attempt for a conversation's most recent run, in the order they
        actually executed — the run_id changes each time a conversation resumes (a new
        AgentLoopRunner.run() call), so this scopes to the latest one rather than
        mixing stages from an earlier attempt into the same pipeline view. Includes
        fallback/retry attempts as separate entries (they get their own sequence
        number in _execute()), which is useful information, not noise, for a status
        dashboard. Used to render the Agent Loop stepper — read-only, no artifact
        bodies here (see get_turn_detail() for that)."""
        with self._connection() as conn:
            latest_run = conn.execute(
                "SELECT run_id FROM turns WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if not latest_run:
                return []
            rows = conn.execute(
                "SELECT id, sequence, stage, profile_id, provider, model, state, "
                "attempt_index, created_at FROM turns WHERE conversation_id = ? AND run_id = ? "
                "ORDER BY sequence ASC, attempt_index ASC",
                (conversation_id, latest_run[0]),
            ).fetchall()
        return [
            {
                "turn_id": row[0],
                "sequence": row[1],
                "stage": row[2],
                "profile_id": row[3],
                "provider": row[4],
                "model": row[5],
                "state": row[6],
                "attempt_index": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    def get_turn_detail(self, turn_id: int) -> dict | None:
        """Full detail for one stage attempt: the parsed structured artifact (the
        decision/plan/verdict itself) plus the model's raw final response text, for
        the status dashboard's "click a stage to see what happened" view. Looks up
        model_responses by (run_id, sequence, attempt_index) — the same triple
        save_stage_result() writes both rows under — to find the matching response_json
        without needing a foreign key column added just for this."""
        with self._connection() as conn:
            turn = conn.execute(
                "SELECT id, conversation_id, run_id, sequence, stage, profile_id, "
                "attempt_index, provider, model, state, artifact_json, created_at "
                "FROM turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if not turn:
                return None
            response_row = conn.execute(
                "SELECT response_json FROM model_responses "
                "WHERE run_id = ? AND sequence = ? AND attempt_index = ? LIMIT 1",
                (turn[2], turn[3], turn[6]),
            ).fetchone()
        final_text = None
        if response_row:
            try:
                final_text = json.loads(response_row[0]).get("final_text")
            except (json.JSONDecodeError, AttributeError):
                final_text = None
        artifact = None
        if turn[10]:
            try:
                artifact = json.loads(turn[10])
            except json.JSONDecodeError:
                artifact = None
        return {
            "turn_id": turn[0],
            "conversation_id": turn[1],
            "run_id": turn[2],
            "sequence": turn[3],
            "stage": turn[4],
            "profile_id": turn[5],
            "attempt_index": turn[6],
            "provider": turn[7],
            "model": turn[8],
            "state": turn[9],
            "artifact": artifact,
            "final_text": final_text,
            "created_at": turn[11],
        }

    def get_workspace_lock(self, workspace_id: str) -> dict | None:
        """The oldest running task of a workspace, or None when nothing is running.
        A workspace can now hold several (see list_workspace_locks)."""
        locks = self.list_workspace_locks(workspace_id)
        return locks[0] if locks else None

    def create_schedule(
        self,
        *,
        project: str,
        workspace_id: str,
        goal: str,
        kind: str,
        next_run_at: datetime,
        interval_seconds: int | None = None,
        time_of_day: str | None = None,
        end_time_of_day: str | None = None,
        chat_id: int | None = None,
    ) -> str:
        schedule_id = f"sched_{uuid4().hex[:10]}"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO schedules (
                    id, project, workspace_id, goal, kind, interval_seconds,
                    time_of_day, end_time_of_day, chat_id, active, created_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    schedule_id, project, workspace_id, goal, kind, interval_seconds,
                    time_of_day, end_time_of_day, chat_id, utc_now_iso(), next_run_at.isoformat(),
                ),
            )
        return schedule_id

    @staticmethod
    def _schedule_row_to_dict(row: tuple) -> dict:
        return {
            "id": row[0],
            "project": row[1],
            "workspace_id": row[2],
            "goal": row[3],
            "kind": row[4],
            "interval_seconds": row[5],
            "time_of_day": row[6],
            "end_time_of_day": row[7],
            "chat_id": row[8],
            "active": bool(row[9]),
            "created_at": row[10],
            "next_run_at": row[11],
            "last_run_at": row[12],
            "last_run_status": row[13],
            "paused_until": row[14],
        }

    _SCHEDULE_COLUMNS = (
        "id, project, workspace_id, goal, kind, interval_seconds, time_of_day, end_time_of_day, "
        "chat_id, active, created_at, next_run_at, last_run_at, last_run_status, paused_until"
    )

    def get_schedule(self, schedule_id: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                f"SELECT {self._SCHEDULE_COLUMNS} FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._schedule_row_to_dict(row) if row else None

    def list_schedules(self, project: str, *, active_only: bool = True) -> list[dict]:
        query = f"SELECT {self._SCHEDULE_COLUMNS} FROM schedules WHERE project = ?"
        params: tuple = (project,)
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY next_run_at ASC"
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._schedule_row_to_dict(row) for row in rows]

    def list_due_schedules(self, now: datetime) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT {self._SCHEDULE_COLUMNS} FROM schedules "
                "WHERE active = 1 AND next_run_at <= ? "
                "AND (paused_until IS NULL OR paused_until <= ?) ORDER BY next_run_at ASC",
                (now.isoformat(), now.isoformat()),
            ).fetchall()
        return [self._schedule_row_to_dict(row) for row in rows]

    def deactivate_schedule(self, schedule_id: str) -> bool:
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE schedules SET active = 0 WHERE id = ? AND active = 1", (schedule_id,)
            )
            return cur.rowcount > 0

    def pause_schedule(self, schedule_id: str, *, paused_until: datetime, next_run_at: datetime) -> bool:
        """Keep the row (so it can be resumed) but stop it firing until `paused_until`;
        next_run_at is moved past the pause so it doesn't fire immediately on expiry."""
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE schedules SET paused_until = ?, next_run_at = ? WHERE id = ? AND active = 1",
                (paused_until.isoformat(), next_run_at.isoformat(), schedule_id),
            )
            return cur.rowcount > 0

    def resume_schedule(self, schedule_id: str, *, next_run_at: datetime) -> bool:
        with self._connection() as conn:
            cur = conn.execute(
                "UPDATE schedules SET paused_until = NULL, next_run_at = ? WHERE id = ? AND active = 1",
                (next_run_at.isoformat(), schedule_id),
            )
            return cur.rowcount > 0

    def mark_schedule_ran(
        self, schedule_id: str, *, ran_at: datetime, next_run_at: datetime, status: str
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE schedules SET last_run_at = ?, last_run_status = ?, next_run_at = ? WHERE id = ?",
                (ran_at.isoformat(), status, next_run_at.isoformat(), schedule_id),
            )

    def get_telegram_offset(self, bot_token_hash: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT last_offset FROM telegram_gateway_state WHERE bot_token_hash = ?",
                (bot_token_hash,),
            ).fetchone()
        return row[0] if row else 0

    def set_telegram_offset(self, bot_token_hash: str, offset: int) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO telegram_gateway_state (bot_token_hash, last_offset)
                VALUES (?, ?)
                ON CONFLICT(bot_token_hash) DO UPDATE SET last_offset = excluded.last_offset
                """,
                (bot_token_hash, offset),
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _usage_units(usage) -> int:
    if usage.total_tokens is not None:
        return int(usage.total_tokens)
    return int(usage.input_tokens + usage.output_tokens)


def _split_memory_ref(default_workspace: str, ref: str) -> tuple[str | None, str | None]:
    if "/" in ref:
        workspace_id, node_id = ref.split("/", 1)
        if workspace_id.startswith("ws_") and node_id.startswith("mem_"):
            return workspace_id, node_id
    if ref.startswith("mem_"):
        return default_workspace, ref
    return None, None
