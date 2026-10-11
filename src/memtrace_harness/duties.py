"""The Harness's background duties: what it does on its own, between conversations.

The nightly memory digest, knowledge-base tidying and promotion, the review of its own work,
the cold-memory consolidation. Each takes the one `HarnessService` of this workspace (or None)
to report through; none of them knows how a report reaches a person.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from memtrace_harness.cli_process import CliProcessRunner
from memtrace_harness.config import HarnessConfig
from memtrace_harness.decision_records import precedent_block
from memtrace_harness.kb_gardening import (
    GARDEN_TIMEOUT_SECONDS,
    garden_due,
    run_gardening_pass,
    workspaces_to_garden,
)
from memtrace_harness.kb_promotion import PROMOTION_TIMEOUT_SECONDS, run_promotion_pass
from memtrace_harness.memory_audit import AuditOutcome, due_audit_dates, run_audit_pass
from memtrace_harness.memory_digest import (
    AUTO_CATCH_UP_DAYS,
    DigestError,
    PreferenceChanges,
    due_digest_dates,
    preference_context_for,
    run_digest_for_date,
    sync_digests_to_memtrace,
    sync_operator_profile,
)
from memtrace_harness.memory_topics import (
    MAX_DAYS_PER_PASS,
    due_topic_dates,
    finish_archived_days,
    run_topics_for_date,
    sync_topics_to_memtrace,
)
from memtrace_harness.memory_reorg import (
    ReorgOutcome,
    finish_rebuild,
    retire_covered_drafts,
    run_review_pass,
    start_rebuild,
)
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.service import HarnessService
from memtrace_harness.trace_store import TraceStore
from memtrace_harness.trigger_review import make_review_caller
from memtrace_harness.work_review import (
    MIN_RUNS_FOR_AUTO_REVIEW,
    REVIEW_TIMEOUT_SECONDS as WORK_REVIEW_TIMEOUT_SECONDS,
    review_due,
    run_work_review,
    write_lessons,
    write_review_index,
)

logger = logging.getLogger(__name__)

# How often the Harness loop checks whether a nightly memory digest is due. Cheap when
# nothing is due (SQLite reads only); the digest itself runs in its own thread so a
# multi-minute model call never stalls the Harness's own tick.
DIGEST_CHECK_SECONDS = 600
# A project-day whose digest failed (provider down, unparseable answer) is retried at
# most this often, instead of on every check.
DIGEST_RETRY_SECONDS = 3600
DIGEST_TIMEOUT_SECONDS = 300
_digest_failures: dict[tuple[str, str], float] = {}


def make_digest_model_caller(config: HarnessConfig, project_name: str):
    """One tool-less call through that project's digest candidates (its own model,
    independent of chat; chat candidates only when none is configured), in order. Runs in a
    neutral empty directory (like Controller's sandbox) rather than the project repo,
    so a provider CLI's own repo-scoped instructions or auto-memory can't leak into
    the digest."""
    candidates = config.digest_candidates_for(project_name)
    sandbox = (config.trace_root / "digest-workspace").resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    def call(prompt: str) -> tuple[str, str | None, str | None]:
        errors = []
        for provider, model in candidates:
            cmd = config.chat_command(provider, model, prompt)
            result = CliProcessRunner().run(cmd, cwd=sandbox, timeout_seconds=DIGEST_TIMEOUT_SECONDS)
            if result.return_code == 0 and result.stdout.strip():
                return result.stdout, provider, model
            errors.append(f"{provider}/{model}: exit {result.return_code}")
        raise DigestError("no digest candidate produced a digest (" + "; ".join(errors) + ")")

    return call


ARCHIVE_TIMEOUT_SECONDS = 600


def make_archive_model_caller(config: HarnessConfig, project_name: str):
    """Like make_digest_model_caller(), through that project's archive chain (the memory
    archive's own model; the digest's when none is configured): the nightly split into matters,
    the weekly matter review, the rebuild. A day with many subjects is a long prompt, so the
    timeout is longer than the digest's."""
    candidates = config.archive_candidates_for(project_name)
    sandbox = (config.trace_root / "digest-workspace").resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    def call(prompt: str) -> tuple[str, str | None, str | None]:
        errors = []
        for provider, model in candidates:
            cmd = config.chat_command(provider, model, prompt)
            result = CliProcessRunner().run(cmd, cwd=sandbox, timeout_seconds=ARCHIVE_TIMEOUT_SECONDS)
            if result.return_code == 0 and result.stdout.strip():
                return result.stdout, provider, model
            errors.append(f"{provider}/{model}: exit {result.return_code}")
        raise DigestError("no archive candidate answered (" + "; ".join(errors) + ")")

    return call


AUDIT_TIMEOUT_SECONDS = 600


def make_audit_model_caller(config: HarnessConfig, project_name: str):
    """Like make_digest_model_caller(), through that project's audit chain (the nightly audit's
    own model; the digest's when none is configured). No tools: it is shown the day's log and the
    numbers, and the Harness does every write."""
    candidates = config.audit_candidates_for(project_name)
    sandbox = (config.trace_root / "digest-workspace").resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    def call(prompt: str) -> tuple[str, str | None, str | None]:
        errors = []
        for provider, model in candidates:
            cmd = config.chat_command(provider, model, prompt)
            result = CliProcessRunner().run(cmd, cwd=sandbox, timeout_seconds=AUDIT_TIMEOUT_SECONDS)
            if result.return_code == 0 and result.stdout.strip():
                return result.stdout, provider, model
            errors.append(f"{provider}/{model}: exit {result.return_code}")
        raise DigestError("no audit candidate answered (" + "; ".join(errors) + ")")

    return call


def make_recall_model_caller(config: HarnessConfig, project_name: str):
    """One call through that project's recall candidates (its own stronger model, see
    HarnessConfig.recall_candidates_for), in order. Like the digest it runs in a neutral
    directory so no repo-scoped CLI instructions leak in, but unlike the digest it may use
    MemTrace's read-only tools to dig past the search hits the harness already supplied."""
    from memtrace_harness.topic_recall import RECALL_TIMEOUT_SECONDS, RecallError

    candidates = config.recall_candidates_for(project_name)
    sandbox = (config.trace_root / "recall-workspace").resolve()
    sandbox.mkdir(parents=True, exist_ok=True)

    def call(prompt: str) -> tuple[str, str | None, str | None]:
        errors = []
        for provider, model in candidates:
            cmd = config.chat_command(
                provider,
                model,
                prompt,
                claude_allowed_tools=HarnessService._CHAT_MEMTRACE_READ_TOOLS,
                memtrace_read=True,
            )
            result = CliProcessRunner().run(cmd, cwd=sandbox, timeout_seconds=RECALL_TIMEOUT_SECONDS)
            if result.return_code == 0 and result.stdout.strip():
                return result.stdout, provider, model
            errors.append(f"{provider}/{model}: exit {result.return_code}")
        raise RecallError("no recall candidate answered (" + "; ".join(errors) + ")")

    return call


def archive_topics(
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    scope,
    workspace_id: str,
    tz: ZoneInfo,
    call_model,
    dates: list[str],
    *,
    respect_retry_backoff: bool = False,
    log=print,
) -> int:
    """Split each of those days into matters (oldest first, stopping at the first failure so a
    matter's history stays chronological), then write what MemTrace is missing. Returns the
    number of matters recorded."""
    recorded = 0
    for d in dates:
        key = ("topics", scope.name, d)
        if respect_retry_backoff and time.monotonic() - _digest_failures.get(key, -DIGEST_RETRY_SECONDS) < DIGEST_RETRY_SECONDS:
            break
        session_id = f"psess_{scope.name}"
        try:
            count = run_topics_for_date(trace_store, scope.name, session_id, workspace_id, d, tz, call_model)
        except Exception as exc:
            _digest_failures[key] = time.monotonic()
            logger.exception(f"[{scope.name}] topic archive for {d} failed; will retry later")
            log(f"[{scope.name}] topic archive {d} failed: {exc}")
            break
        _digest_failures.pop(key, None)
        recorded += count
        log(f"[{scope.name}] archived {d}: {count} matter(s)")
    if memtrace_client is not None:
        try:
            written = sync_topics_to_memtrace(trace_store, memtrace_client, scope.name, workspace_id)
            if written:
                log(f"[{scope.name}] wrote {written} matter note(s) to MemTrace {workspace_id}")
            finish_archived_days(trace_store, scope.name, workspace_id, tz)
        except Exception:
            logger.exception(f"[{scope.name}] syncing matter notes to MemTrace failed; will retry next pass")
    return recorded


def audit_night(
    config: HarnessConfig,
    trace_store: TraceStore,
    scope,
    workspace_id: str,
    tz: ZoneInfo,
    now: datetime,
    *,
    only_date: str | None = None,
    respect_retry_backoff: bool = False,
    log=print,
) -> list[AuditOutcome]:
    """The nightly audit for one project (memory_audit.py), after the digest and the archive have
    filed the day. A failed model call records nothing, so the next pass retries that day."""
    key = ("audit", scope.name)
    if respect_retry_backoff and time.monotonic() - _digest_failures.get(key, -DIGEST_RETRY_SECONDS) < DIGEST_RETRY_SECONDS:
        return []
    try:
        outcomes = run_audit_pass(
            trace_store, scope.name, f"psess_{scope.name}", workspace_id, tz,
            make_audit_model_caller(config, scope.name), now, only_date=only_date,
        )
    except Exception as exc:
        _digest_failures[key] = time.monotonic()
        logger.exception(f"[{scope.name}] nightly audit failed; will retry later")
        log(f"[{scope.name}] audit failed: {exc}")
        return []
    _digest_failures.pop(key, None)
    for outcome in outcomes:
        log(
            f"[{scope.name}] audited {outcome.audit_date or '(verification only)'}: "
            f"{len(outcome.applied)} note(s), {len(outcome.proposals)} proposal(s), {len(outcome.verified)} verified"
        )
    return outcomes


_rebuilding: set[str] = set()
_rebuilding_lock = threading.Lock()


def rebuild_matters(
    config: HarnessConfig, trace_store: TraceStore, memtrace_client: MemTraceClient, scope, *, log=print
) -> ReorgOutcome | None:
    """Derive every matter of this project again from the whole local log (start, or resume an
    interrupted rebuild), then trash the superseded notes. None when it did not finish (the
    reason is logged; running it again resumes) or when one is already running."""
    tz = ZoneInfo(config.schedule_timezone)
    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    session_id = f"psess_{scope.name}"
    with _rebuilding_lock:
        if scope.name in _rebuilding:
            log(f"[{scope.name}] a rebuild is already running")
            return None
        _rebuilding.add(scope.name)
    try:
        run_id, resumed = start_rebuild(trace_store, scope.name, workspace_id)
        log(f"[{scope.name}] {'resuming' if resumed else 'started'} rebuild run {run_id}")
        now = datetime.now(timezone.utc)
        dates = due_topic_dates(trace_store, scope.name, session_id, tz, now, max_days_back=None)
        archive_topics(
            trace_store, memtrace_client, scope, workspace_id, tz, make_archive_model_caller(config, scope.name), dates, log=log
        )
        remaining = due_topic_dates(trace_store, scope.name, session_id, tz, now, max_days_back=None)
        if remaining or trace_store.topics_needing_sync(scope.name, workspace_id):
            log(f"[{scope.name}] rebuild not finished ({len(remaining)} day(s) left); run it again to resume")
            return None
        return finish_rebuild(trace_store, memtrace_client, scope.name, workspace_id, run_id)
    finally:
        with _rebuilding_lock:
            _rebuilding.discard(scope.name)


def reorganise_now(
    config: HarnessConfig, trace_store: TraceStore, memtrace_client: MemTraceClient, scope, *, log=print
) -> list[ReorgOutcome]:
    """What the nightly pass would do for the matters, on request: retire the covered old drafts and
    run the matter review now (ignoring the weekly schedule)."""
    tz = ZoneInfo(config.schedule_timezone)
    workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
    outcomes: list[ReorgOutcome] = []
    retired = retire_covered_drafts(trace_store, memtrace_client, scope.name, workspace_id, tz)
    if retired and retired.count:
        outcomes.append(retired)
    reviewed = run_review_pass(
        trace_store, memtrace_client, scope.name, workspace_id, make_archive_model_caller(config, scope.name), force=True
    )
    if reviewed and reviewed.count:
        outcomes.append(reviewed)
    return outcomes


def run_memory_rebuild(
    config, trace_store, memtrace_client, projects, service, *, only_project=None, audience=None, force=True
) -> int:
    """Background body of the /rebuild command (and the chat model's request): rebuild, then tell
    the operator how it ended."""
    ran = 0
    for scope in [p for p in projects if only_project is None or p.name == only_project]:
        if memtrace_client is None:
            service.send_message(audience, "沒有連上 MemTrace，無法重建記憶。")
            continue
        try:
            outcome = rebuild_matters(config, trace_store, memtrace_client, scope)
        except Exception as exc:
            logger.exception(f"[{scope.name}] rebuilding memory failed")
            service.send_message(audience, f"❌ 「{scope.name}」重建記憶失敗：{exc}。再說一次「重建記憶」會從中斷的地方接著做。")
            continue
        if outcome is None:
            service.send_message(
                audience, f"「{scope.name}」的重建還沒完成（或已經有一個在跑）。完成前舊的記憶節點都還在；再說一次「重建記憶」會接著做。"
            )
            continue
        ran += 1
        service.notify_reorg_outcome(outcome, audience)
    return ran


def run_memory_reorg(
    config, trace_store, memtrace_client, projects, service, *, only_project=None, audience=None, force=True
) -> int:
    """Background body of the /reorganize command (and the chat model's request)."""
    ran = 0
    for scope in [p for p in projects if only_project is None or p.name == only_project]:
        if memtrace_client is None:
            service.send_message(audience, "沒有連上 MemTrace，無法整理記憶。")
            continue
        try:
            outcomes = reorganise_now(config, trace_store, memtrace_client, scope)
        except Exception as exc:
            logger.exception(f"[{scope.name}] reorganising memory failed")
            service.send_message(audience, f"❌ 「{scope.name}」整理記憶失敗：{exc}")
            continue
        if not outcomes:
            service.send_message(audience, f"「{scope.name}」的記憶看過了，沒有需要調整的地方。")
            continue
        ran += 1
        for outcome in outcomes:
            service.notify_reorg_outcome(outcome, audience)
    return ran


def _reorganise(
    trace_store, memtrace_client, scope, workspace_id, tz, call_model, *, respect_retry_backoff, log, reorg_out
) -> None:
    """After the nightly archive: retire the old drafts the matters now cover, and (weekly)
    let the model review the matters. Each part is best-effort and reported."""
    try:
        retired = retire_covered_drafts(trace_store, memtrace_client, scope.name, workspace_id, tz)
        if retired:
            log(f"[{scope.name}] {retired.lines[0]}")
            if reorg_out is not None and retired.count:
                reorg_out.append(retired)
    except Exception:
        logger.exception(f"[{scope.name}] retiring old drafts failed; will retry next pass")
    key = ("review", scope.name)
    if respect_retry_backoff and time.monotonic() - _digest_failures.get(key, -DIGEST_RETRY_SECONDS) < DIGEST_RETRY_SECONDS:
        return
    try:
        reviewed = run_review_pass(trace_store, memtrace_client, scope.name, workspace_id, call_model)
    except Exception:
        _digest_failures[key] = time.monotonic()
        logger.exception(f"[{scope.name}] reviewing the matters failed; will retry later")
        return
    _digest_failures.pop(key, None)
    if reviewed:
        log(f"[{scope.name}] matter review: {reviewed.count} change(s)")
        if reorg_out is not None and reviewed.count:
            reorg_out.append(reviewed)


def run_memory_digests(
    config: HarnessConfig,
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    projects: list,
    *,
    max_days_back: int | None,
    only_date: str | None = None,
    dry_run: bool = False,
    respect_retry_backoff: bool = False,
    log=print,
    triage_out: dict[str, list[dict]] | None = None,
    reorg_out: list[ReorgOutcome] | None = None,
    audit_out: list[AuditOutcome] | None = None,
) -> dict[str, PreferenceChanges]:
    """Shared by the Harness's nightly trigger and the `memory-digest` command.
    Returns {project: the preferences the Harness adopted / retired / left waiting}. One
    project-day failing never stops the others; MemTrace sync is best-effort and retried
    next pass."""
    tz = ZoneInfo(config.schedule_timezone)
    now = datetime.now(timezone.utc)
    changed: dict[str, PreferenceChanges] = {}
    for scope in projects:
        session_id = f"psess_{scope.name}"
        if only_date is not None:
            dates = [only_date]
        else:
            dates = due_digest_dates(trace_store, scope.name, session_id, tz, now, max_days_back=max_days_back)
        if dry_run:
            for d in dates:
                log(f"[{scope.name}] would digest {d}")
            for d in (
                [only_date] if only_date is not None
                else due_topic_dates(trace_store, scope.name, session_id, tz, now, max_days_back=None)[:MAX_DAYS_PER_PASS]
            ):
                log(f"[{scope.name}] would archive {d} into matters")
            for d in ([only_date] if only_date is not None else due_audit_dates(trace_store, scope.name, session_id, tz, now)):
                log(f"[{scope.name}] would audit {d}")
            continue
        call_model = make_digest_model_caller(config, scope.name)
        for d in dates:
            key = (scope.name, d)
            if respect_retry_backoff and time.monotonic() - _digest_failures.get(key, -DIGEST_RETRY_SECONDS) < DIGEST_RETRY_SECONDS:
                continue
            try:
                outcome = run_digest_for_date(trace_store, scope.name, session_id, d, tz, call_model)
            except Exception as exc:
                _digest_failures[key] = time.monotonic()
                logger.exception(f"[{scope.name}] memory digest for {d} failed; will retry later")
                log(f"[{scope.name}] digest {d} failed: {exc}")
                continue
            _digest_failures.pop(key, None)
            total = changed.setdefault(scope.name, PreferenceChanges())
            total.adopted += outcome.preferences.adopted
            total.retired += outcome.preferences.retired
            total.waiting += outcome.preferences.waiting
            if triage_out is not None:
                reviewed = [
                    i for i in outcome.digest["resolved_items"] if i.get("resolution") in ("decided", "stale")
                ] + [i for i in outcome.digest["open_items"] if i.get("needs_operator")]
                if reviewed:
                    triage_out.setdefault(scope.name, []).extend(reviewed)
            log(
                f"[{scope.name}] digested {d}: {outcome.turn_count} turn(s), "
                f"{len(outcome.digest['decisions'])} decision(s), "
                f"{len(outcome.digest['open_items'])} open item(s), "
                f"preferences +{len(outcome.preferences.adopted)} "
                f"-{len(outcome.preferences.retired)} "
                f"(waiting {len(outcome.preferences.waiting)}), "
                f"{outcome.digest['discarded_ungrounded']} ungrounded item(s) discarded "
                f"({outcome.provider}/{outcome.model})"
            )
        workspace_id = config.memory_workspace_id_for(scope.name, scope.workspace_id)
        if only_date is not None:
            topic_dates = [only_date]
        else:
            # Not limited to max_days_back: the matters have no catch-up window, only a per-night cap.
            topic_dates = due_topic_dates(trace_store, scope.name, session_id, tz, now, max_days_back=None)[:MAX_DAYS_PER_PASS]
        call_archive = make_archive_model_caller(config, scope.name)
        archive_topics(
            trace_store, memtrace_client, scope, workspace_id, tz, call_archive, topic_dates,
            respect_retry_backoff=respect_retry_backoff, log=log,
        )
        if memtrace_client is not None and only_date is None:
            _reorganise(
                trace_store, memtrace_client, scope, workspace_id, tz, call_archive,
                respect_retry_backoff=respect_retry_backoff, log=log, reorg_out=reorg_out,
            )
        outcomes = audit_night(
            config, trace_store, scope, workspace_id, tz, now,
            only_date=only_date, respect_retry_backoff=respect_retry_backoff, log=log,
        )
        if audit_out is not None:
            audit_out.extend(outcomes)
        if memtrace_client is not None:
            try:
                synced = sync_digests_to_memtrace(trace_store, memtrace_client, scope.name, workspace_id, tz)
                if synced:
                    log(f"[{scope.name}] wrote {synced} digest(s) to MemTrace {workspace_id}")
            except Exception:
                logger.exception(f"[{scope.name}] syncing digests to MemTrace failed; will retry next pass")
    if (
        memtrace_client is not None
        and config.operator_preference_workspace_id
        and any(c.adopted or c.retired for c in changed.values())
    ):
        try:
            sync_operator_profile(trace_store, memtrace_client, config.operator_preference_workspace_id)
        except Exception:
            logger.exception("mirroring the operator profile to MemTrace failed; will retry on the next change")
    return changed


def _describe_preference_changes(changes: PreferenceChanges, limit: int = 8) -> list[str]:
    lines = [f"- [#{r['id']}] {r['text']}" for r in changes.adopted[:limit]]
    lines += [f"- 撤回 [#{r['id']}] {r['text']}（{r['retire_reason'] or ''}）" for r in changes.retired[:limit]]
    hidden = len(changes.adopted) + len(changes.retired) - len(lines)
    if hidden > 0:
        lines.append(f"…另外 {hidden} 條")
    return lines


def _describe_open_item_triage(project: str, items: list[dict]) -> str:
    decided = [i for i in items if i.get("resolution") == "decided"]
    stale = [i for i in items if i.get("resolution") == "stale"]
    asking = [i for i in items if i.get("needs_operator")]
    lines = [f"🗂 [{project}] 昨晚整理了未結事項，想跟你確認："]
    if decided:
        lines.append("已決議（我已關閉）：")
        lines += [f"- {i['text']} → {i.get('resolution_reason', '')}" for i in decided[:10]]
    if stale:
        lines.append("已過時（我已關閉）：")
        lines += [f"- {i['text']} → {i.get('resolution_reason', '')}" for i in stale[:10]]
    if asking:
        lines.append("需要你決定：")
        lines += [f"- {i['text']} → {i.get('operator_question', '')}" for i in asking[:12]]
    lines.append("我關錯的請直接回覆告訴我，我會重新打開；要決定的事回覆我，我們可以一起討論。")
    return "\n".join(lines)


def run_knowledge_gardening(
    config: HarnessConfig,
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    projects: list,
    service: HarnessService | None,
    *,
    only_project: str | None = None,
    force: bool = False,
    audience: str | None = None,
) -> int:
    """Tidy and re-map the knowledge-base workspaces of the harness's projects (kb_gardening.py):
    the ones that are due (not tidied for a week), or all of a project's when forced. One
    workspace failing never stops the others. Returns how many passes ran."""
    if memtrace_client is None:
        return 0
    ran = 0
    scopes = [p for p in projects if only_project is None or p.name == only_project]
    for workspace_id, project, label, purpose in workspaces_to_garden(config, scopes):
        if not force and not garden_due(trace_store, workspace_id):
            continue
        try:
            outcome = run_gardening_pass(
                client=memtrace_client,
                trace_store=trace_store,
                workspace_id=workspace_id,
                project=project,
                role_label=label,
                purpose=purpose,
                caller=make_review_caller(config, project, timeout_seconds=GARDEN_TIMEOUT_SECONDS),
            )
        except Exception:
            logger.exception(f"[{project}] tidying {workspace_id} failed; will retry next pass")
            continue
        if outcome is None:
            continue
        ran += 1
        gw = service
        if gw is not None:
            try:
                gw.notify_garden_outcome(outcome, audience)
            except Exception:
                logger.exception(f"[{project}] reporting the tidying pass failed")
    return ran


def run_knowledge_promotion(
    config: HarnessConfig,
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    projects: list,
    service: HarnessService | None,
    *,
    only_project: str | None = None,
    audience: str | None = None,
    force: bool = False,
) -> int:
    """Promote a project's cold-memory findings into its specification workspace as conclusion
    notes and an overview (kb_promotion.py). On demand (/promote, force=True: always runs and
    always reports) or from the nightly window (due weekly, and only when the digests have
    changed since the last pass; the operator is told only if something was written or flagged).
    Returns the passes run."""
    if memtrace_client is None:
        return 0
    ran = 0
    for scope in projects:
        if only_project is not None and scope.name != only_project:
            continue
        if not force and not garden_due(trace_store, scope.workspace_id, kind="promote"):
            continue
        try:
            outcome = run_promotion_pass(
                client=memtrace_client,
                trace_store=trace_store,
                project=scope.name,
                spec_workspace_id=scope.workspace_id,
                memory_workspace_id=config.memory_workspace_id_for(scope.name, scope.workspace_id),
                purpose=scope.raw_markdown,
                caller=make_review_caller(config, scope.name, timeout_seconds=PROMOTION_TIMEOUT_SECONDS),
                only_if_new=not force,
            )
        except Exception:
            logger.exception(f"[{scope.name}] promoting research conclusions failed")
            gw = service
            if gw is not None and audience is not None:
                gw.send_message(audience, "📚 整理研究結論時發生錯誤，請看日誌。")
            continue
        gw = service
        if outcome is None:
            if gw is not None and audience is not None:
                gw.send_message(audience, f"📚 [{scope.name}] 沒有可整理的來源（沒有獨立的冷記憶工作區、沒有每日摘要，或模型沒有給出可用的答案）。")
            continue
        ran += 1
        wrote = bool(outcome.created or outcome.updated or outcome.index or outcome.stale or outcome.dropped)
        if gw is not None and (force or wrote):
            try:
                gw.notify_promotion_outcome(outcome, audience)
            except Exception:
                logger.exception(f"[{scope.name}] reporting the promotion pass failed")
    return ran


def run_work_reviews(
    config: HarnessConfig,
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    projects: list,
    service: HarnessService | None,
    *,
    only_project: str | None = None,
    audience: str | None = None,
    force: bool = False,
) -> int:
    """Review how the harness's own work went since the last review (work_review.py), one review
    per workspace (projects sharing a workspace are reviewed together). On demand (/review or a
    request in chat; force=True) or weekly from the nightly window, after a first review exists and
    when the period holds enough runs. Returns the reviews made."""
    ran = 0
    seen: set[str] = set()
    for scope in projects:
        if only_project is not None and scope.name != only_project:
            continue
        workspace_id = scope.workspace_id
        if workspace_id in seen:
            continue
        seen.add(workspace_id)
        if not force and not review_due(trace_store, workspace_id):
            continue
        gw = service
        try:
            outcome = run_work_review(
                trace_store=trace_store,
                workspace_id=workspace_id,
                project=scope.name,
                project_names=[p.name for p in projects if p.workspace_id == workspace_id],
                caller=make_review_caller(config, scope.name, timeout_seconds=WORK_REVIEW_TIMEOUT_SECONDS),
                precedent=precedent_block(
                    trace_store, scope.name,
                    preferences=preference_context_for(trace_store, scope.name, with_ids=True),
                ),
                min_runs=0 if force else MIN_RUNS_FOR_AUTO_REVIEW,
            )
        except Exception:
            logger.exception(f"[{scope.name}] the work review failed")
            outcome = None
        if outcome is None:
            if gw is not None and audience is not None:
                gw.send_message(audience, f"🔍 [{scope.name}] 這段期間沒有足夠的紀錄可以復盤，或模型沒有給出可用的答案。")
            continue
        if memtrace_client is not None:
            memory_ws = config.memory_workspace_id_for(scope.name, workspace_id)
            write_lessons(memtrace_client, trace_store, outcome, memory_ws)
            write_review_index(memtrace_client, trace_store, outcome, memory_ws)
        ran += 1
        if gw is not None:
            try:
                gw.notify_review_outcome(outcome, scope, audience)
            except Exception:
                logger.exception(f"[{scope.name}] reporting the work review failed")
    return ran


def _run_nightly_digest_pass(
    config: HarnessConfig,
    trace_store: TraceStore,
    memtrace_client: MemTraceClient | None,
    projects: list,
    *,
    service: HarnessService | None,
    on_change=None,
) -> None:
    triage: dict[str, list[dict]] = {}
    reorg: list[ReorgOutcome] = []
    audits: list[AuditOutcome] = []
    try:
        changed = run_memory_digests(
            config,
            trace_store,
            memtrace_client,
            projects,
            max_days_back=AUTO_CATCH_UP_DAYS,
            respect_retry_backoff=True,
            triage_out=triage,
            reorg_out=reorg,
            audit_out=audits,
        )
    except Exception:
        logger.exception("nightly memory digest pass failed")
        return
    if on_change is not None:
        on_change()
    if service is None:
        return
    for outcome in reorg:
        try:
            service.notify_reorg_outcome(outcome)
        except Exception:
            logger.exception(f"[{outcome.project}] reporting the memory reorganisation failed")
    for outcome in audits:
        try:
            service.notify_audit_outcome(outcome)
        except Exception:
            logger.exception(f"[{outcome.project}] reporting the nightly audit failed")
    for project, items in triage.items():
        text = _describe_open_item_triage(project, items)
        try:
            service.notify_operator(text)
            # Into the chat log too, so the chat model can discuss it when the operator replies.
            service.primary_session_mgr.record_turn(
                project=project, speaker="assistant", turn_type="chat", content=text
            )
        except Exception:
            logger.exception(f"[{project}] notifying about the open-item review failed")
    merged = PreferenceChanges()
    for changes in changed.values():
        merged.adopted += changes.adopted
        merged.retired += changes.retired
        merged.waiting += changes.waiting
    if not (merged.adopted or merged.retired or merged.waiting):
        return
    lines = ["🧠 昨晚的記憶整理自動調整了你的偏好："]
    lines += _describe_preference_changes(merged)
    if merged.waiting:
        lines.append(f"另有 {len(merged.waiting)} 條超過每日自動採用上限，等你到狀態網頁確認。")
    lines.append("不同意的話，直接在聊天裡說（例如「那個不用了」），或到狀態網頁撤銷。")
    try:
        service.notify_operator("\n".join(lines))
    except Exception:
        logger.exception("notifying about preference changes failed")
