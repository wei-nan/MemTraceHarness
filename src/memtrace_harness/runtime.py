"""Run one Harness: its service, its API, and the housekeeping it does on its own.

`memtrace-harness serve` is this module. It owns what used to be the Harness's serve loop minus
everything that talked to a chat: the timed duties (backlog scan, task-queue drain, orders and job
requests, approval expiry, due schedules, cold-memory consolidation, the nightly digest and its
neighbours) plus the local HTTP API (api.py) that services use to reach it.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from memtrace_harness.api import ChangeBus, HarnessApi
from memtrace_harness.approval import ApprovalManager
from memtrace_harness.chat_triage import ChatTriage
from memtrace_harness.config import HarnessConfig
from memtrace_harness.duties import (
    DIGEST_CHECK_SECONDS,
    _make_chat_classifier,
    _run_nightly_digest_pass,
    make_recall_model_caller,
    run_knowledge_gardening,
    run_knowledge_promotion,
    run_work_reviews,
)
from memtrace_harness.memory_digest import in_digest_window
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.primary_session import PrimarySessionManager
from memtrace_harness.scanner import UnattendedScanner
from memtrace_harness.schedule import (
    compute_next_run,
    is_outside_interval_window,
    is_past_window_end,
    spec_from_row,
)
from memtrace_harness.scope import ProjectScope
from memtrace_harness.service import HarnessService
from memtrace_harness.topic_recall import TopicRecallService
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.trigger_review import make_review_caller

logger = logging.getLogger(__name__)

# How often the loop wakes up to see whether anything is due.
MAIN_LOOP_TICK_SECONDS = 1.0
# How often the loop looks for pending approvals that have outlived their TTL.
APPROVAL_EXPIRY_CHECK_SECONDS = 600
# Delivered outbox events are kept this long (for the status page and debugging), then pruned.
OUTBOX_RETENTION_HOURS = 48


@dataclass
class HarnessParts:
    config: HarnessConfig
    scope: ProjectScope
    trace_store: TraceStore
    approval_manager: ApprovalManager
    memtrace_client: MemTraceClient | None
    primary_session_mgr: PrimarySessionManager
    service: HarnessService
    scanner: UnattendedScanner


def build_harness(config: HarnessConfig, scope: ProjectScope) -> HarnessParts:
    """Wire one Harness: the store, the service and the background helpers the service starts
    on request (/garden, /promote, /review, topic recall)."""
    trace_store = TraceStore(config.trace_db_path)
    approval_mgr = ApprovalManager(trace_store)
    memtrace_client = (
        MemTraceClient(config.memtrace_mcp_url, config.memtrace_api_token) if config.memtrace_mcp_url else None
    )
    primary_session_mgr = PrimarySessionManager(trace_store, memtrace_client)
    projects = [scope]
    service = HarnessService(
        config, approval_mgr, ChatTriage(projects), primary_session_mgr, projects, memtrace_client
    )

    def record_push(project: str, text: str) -> None:
        primary_session_mgr.record_turn(project=project, speaker="assistant", turn_type="chat", content=text)

    service.topic_recall = TopicRecallService(
        config,
        trace_store,
        memtrace_client,
        call_model_factory=lambda project: make_recall_model_caller(config, project),
        send=service.send_message,
        record_push=record_push,
    )
    service.review_caller_factory = lambda project: make_review_caller(config, project)

    def start(target, name: str, **kwargs):
        def go(scope_: ProjectScope, audience: str | None) -> None:
            threading.Thread(
                target=target,
                args=(config, trace_store, memtrace_client, projects, service),
                kwargs={"only_project": scope_.name, "audience": audience, "force": True, **kwargs},
                name=f"{name}-{scope_.name}",
                daemon=True,
            ).start()

        return go

    service.garden_now = start(run_knowledge_gardening, "kb-garden")
    service.promote_now = start(run_knowledge_promotion, "kb-promote")
    service.work_review_now = start(run_work_reviews, "work-review")

    scanner = UnattendedScanner(
        config,
        trace_store,
        projects,
        memtrace_client,
        service,
        approval_mgr,
        record_push=record_push,
        review_caller_factory=lambda project: make_review_caller(config, project),
    )
    return HarnessParts(
        config, scope, trace_store, approval_mgr, memtrace_client, primary_session_mgr, service, scanner
    )


def _startup_checks(parts: HarnessParts) -> None:
    """Tell the operator, once, about tooling that is not what the Harness expects."""
    from memtrace_harness.agy_mcp_check import check_antigravity_mcp
    from memtrace_harness.chat_tools_check import check_chat_tools

    config, service = parts.config, parts.service
    try:
        mcp_problems = check_antigravity_mcp(config.memtrace_mcp_url)
    except Exception:
        logger.exception("Antigravity MCP check failed; continuing")
        mcp_problems = []
    for problem in mcp_problems:
        print(f"WARNING: {problem}", flush=True)
        logger.warning(problem)
    if mcp_problems:
        service.notify_operator("⚠️ Antigravity 的 MCP 設定與 Harness 預期不符：\n" + "\n".join(mcp_problems))

    try:
        chat_problems = check_chat_tools(config, [parts.scope.name])
    except Exception:
        logger.exception("chat tool check failed; continuing")
        return
    for problem in chat_problems:
        print(f"WARNING: {problem}", flush=True)
        logger.warning(problem)
    if chat_problems:
        service.notify_operator("⚠️ 聊天模型的 MemTrace 工具與預期不符：\n" + "\n".join(chat_problems))


def serve(
    config: HarnessConfig,
    scope: ProjectScope,
    *,
    scan_interval: int = 1800,
    consolidation_interval: int = 3600,
    schedule_check_interval: int = 30,
) -> int:
    """Run until SIGINT/SIGTERM. A crash in one duty is logged and the loop continues rather than
    taking the whole process down."""
    parts = build_harness(config, scope)
    service, trace_store, primary_session_mgr = parts.service, parts.trace_store, parts.primary_session_mgr
    projects = [scope]
    bus = ChangeBus()
    started_at = datetime.now(timezone.utc).isoformat()

    api = HarnessApi(service, config, scope, bus=bus, started_at=started_at)
    api.start()
    print(
        f"Serving Harness '{scope.name}' (workspace {config.workspace_root}); API on "
        f"http://{config.api_host}:{config.api_port}; scan_interval={scan_interval}s "
        f"consolidation_interval={consolidation_interval}s schedule_check_interval={schedule_check_interval}s. "
        "Ctrl-C to stop.",
        flush=True,
    )

    stop_event = threading.Event()

    def _handle_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    service.notify_operator(f"✅ Harness 已啟動（{scope.name}）。")
    _startup_checks(parts)

    last_scan = 0.0
    last_consolidation = 0.0
    last_digest_check = 0.0
    last_schedule_check = 0.0
    last_approval_expiry = -APPROVAL_EXPIRY_CHECK_SECONDS  # run once right after startup
    digest_thread: threading.Thread | None = None
    garden_thread: threading.Thread | None = None
    promote_thread: threading.Thread | None = None
    review_thread: threading.Thread | None = None
    schedule_tz = ZoneInfo(config.schedule_timezone)
    first_tick = True
    while not stop_event.is_set():
        if not first_tick and stop_event.wait(MAIN_LOOP_TICK_SECONDS):
            break
        first_tick = False

        now = time.monotonic()
        if now - last_scan >= scan_interval:
            try:
                scan_results = parts.scanner.run_scan_pass()
                noteworthy = {k: v for k, v in scan_results.items() if v not in ("no_backlog", "skipped_locked")}
                if noteworthy:
                    print(f"scan pass: {noteworthy}", flush=True)
                    bus.publish()
            except Exception:
                logger.exception("scan pass failed; continuing")
            last_scan = now

        # Picks up queued tasks wherever a worker slot is free — including work queued before a
        # restart; shows orders the model proposed and jobs it asked to re-run (buttons now, the
        # action only after a tap) and closes the ones nobody answered.
        for duty, label in (
            (service.drain_task_queues, "draining the task queue"),
            (service.process_order_intents, "processing proposed orders"),
            (service.process_job_requests, "processing job requests"),
        ):
            try:
                duty()
            except Exception:
                logger.exception(f"{label} failed; continuing")

        if now - last_approval_expiry >= APPROVAL_EXPIRY_CHECK_SECONDS:
            try:
                service.expire_stale_approvals(config.approval_ttl_hours)
                trace_store.prune_outbox(
                    (datetime.now(timezone.utc) - _hours(OUTBOX_RETENTION_HOURS)).isoformat()
                )
            except Exception:
                logger.exception("expiring stale approvals failed; continuing")
            last_approval_expiry = now

        if now - last_schedule_check >= schedule_check_interval:
            try:
                due = trace_store.list_due_schedules(datetime.now(timezone.utc))
                for row in due:
                    # Advance next_run_at before triggering: run_due_schedule() only starts a
                    # background thread (non-blocking), and if the workspace turns out to be locked
                    # it just skips this occurrence — either way the schedule must not re-fire
                    # every tick until it succeeds.
                    spec = spec_from_row(row)
                    ran_at = datetime.now(timezone.utc)
                    next_run_at = compute_next_run(spec, after=ran_at, tz=schedule_tz)
                    if is_past_window_end(spec, at=ran_at, tz=schedule_tz) or is_outside_interval_window(
                        spec, at=ran_at, tz=schedule_tz
                    ):
                        trace_store.mark_schedule_ran(
                            row["id"], ran_at=ran_at, next_run_at=next_run_at, status="skipped_window"
                        )
                        logger.info(
                            f"schedule {row['id']} due but past its {spec.end_time_of_day} window "
                            "end; skipping this run"
                        )
                        continue
                    trace_store.mark_schedule_ran(
                        row["id"], ran_at=ran_at, next_run_at=next_run_at, status="triggered"
                    )
                    try:
                        service.run_due_schedule(row)
                    except Exception:
                        logger.exception(f"scheduled run for {row['id']} failed to start; continuing")
                if due:
                    print(f"triggered {len(due)} due schedule(s)", flush=True)
                    bus.publish()
            except Exception:
                logger.exception("schedule check pass failed; continuing")
            last_schedule_check = now

        if now - last_consolidation >= consolidation_interval:
            try:
                memory_workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
                classify_fn = _make_chat_classifier(config, scope.name, scope.working_directory)
                written = primary_session_mgr.consolidate_to_memtrace(
                    scope.name, memory_workspace_id, classify_fn, tz=schedule_tz
                )
                if written:
                    print(f"[{scope.name}] consolidated {len(written)} turn(s) to MemTrace as draft evidence", flush=True)
                    bus.publish()
            except Exception:
                logger.exception(f"[{scope.name}] cold-memory consolidation failed; will retry next cycle")
            last_consolidation = now

        if now - last_digest_check >= DIGEST_CHECK_SECONDS:
            in_window = in_digest_window(datetime.now(schedule_tz))
            client = primary_session_mgr.memtrace_client
            if (digest_thread is None or not digest_thread.is_alive()) and in_window:
                digest_thread = threading.Thread(
                    target=_run_nightly_digest_pass,
                    args=(config, trace_store, client, projects),
                    kwargs={"service": service, "on_change": bus.publish},
                    name="memory-digest",
                    daemon=True,
                )
                digest_thread.start()
            # Same night window as the digest; each workspace is only actually tidied if a week
            # has passed since its last successful pass (kb_gardening.garden_due).
            if (garden_thread is None or not garden_thread.is_alive()) and in_window:
                garden_thread = threading.Thread(
                    target=run_knowledge_gardening,
                    args=(config, trace_store, client, projects, service),
                    name="kb-garden",
                    daemon=True,
                )
                garden_thread.start()
            # Promoting findings into the specification workspace reads the digests, so it waits
            # for tonight's digest pass to finish. Due weekly, and only if the digests changed.
            if (
                (promote_thread is None or not promote_thread.is_alive())
                and (digest_thread is None or not digest_thread.is_alive())
                and in_window
            ):
                promote_thread = threading.Thread(
                    target=run_knowledge_promotion,
                    args=(config, trace_store, client, projects, service),
                    name="kb-promote",
                    daemon=True,
                )
                promote_thread.start()
            # The weekly review of the harness's own work (only after a first one was asked for).
            if (review_thread is None or not review_thread.is_alive()) and in_window:
                review_thread = threading.Thread(
                    target=run_work_reviews,
                    args=(config, trace_store, client, projects, service),
                    name="work-review",
                    daemon=True,
                )
                review_thread.start()
            last_digest_check = now

    # One shutdown budget: let in-flight Agent Loop runs finish within the grace period. Stays
    # inside launchd's ExitTimeOut.
    deadline = time.monotonic() + config.shutdown_grace_seconds
    drain_inflight_agent_loops(max(1, int(deadline - time.monotonic())))
    api.stop()
    print("Harness serve loop stopped.", flush=True)
    return 0


def _hours(n: int):
    from datetime import timedelta

    return timedelta(hours=n)


def drain_inflight_agent_loops(grace_seconds: int) -> None:
    """Background Agent Loop threads (chat tasks, approval resumes, unattended scan backlog runs)
    are daemon threads — the interpreter doesn't wait for them or guarantee their `finally` blocks
    run on exit, so without this, killing the process mid-run leaves that workspace's lock stuck
    forever. Wait up to grace_seconds for any in-flight runs to finish normally; if they don't,
    force-kill the underlying CLI subprocess that thread is currently blocked on (via
    cli_process.py's thread-keyed process registry) and force-release the workspace lock so the
    next start isn't blocked. The interrupted run's outcome is still unknown either way — this
    makes shutdown bounded and clean, not silent."""
    from memtrace_harness.cli_process import kill_process_for_thread
    from memtrace_harness.inflight import default_tracker

    remaining = default_tracker.snapshot()
    if not remaining:
        return
    print(
        f"Waiting up to {grace_seconds}s for {len(remaining)} in-flight Agent Loop "
        "run(s) to finish before exiting...",
        flush=True,
    )
    if default_tracker.wait_for_drain(grace_seconds):
        return
    stuck = default_tracker.snapshot()
    for entry in stuck:
        thread_ident = entry["thread"].ident
        killed = thread_ident is not None and kill_process_for_thread(thread_ident)
        logger.warning(
            f"Shutdown grace period exceeded; {'killed the CLI subprocess and ' if killed else ''}"
            f"force-releasing workspace lock for {entry['workspace_id']} "
            f"(conversation {entry['conversation_id']}) — its Agent Loop run was "
            "interrupted mid-flight and its outcome is unknown. Check the trace store "
            "/ raw trace logs once the new process is up."
        )
        try:
            entry["trace_store"].release_workspace_lock(
                entry["workspace_id"], entry["conversation_id"]
            )
        except Exception:
            logger.exception("failed to force-release workspace lock during shutdown")
