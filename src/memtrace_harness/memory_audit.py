"""The nightly audit: the Harness checks how the day went between the operator and itself, and
learns from it.

Contract: "Nightly audit" in docs/operating-contract.md. The North Star is *the operator reaches what
they want with the least repetition and round-tripping*. Everything here serves one question: what in
yesterday's conversation, and in how it was filed into memory, cost the operator effort that a better
Harness would have saved?

Two halves, deliberately kept apart so the judgement is not the model's alone:

* **Metrics** (`compute_metrics`) are mechanical. They count the operator's corrections, restated
  messages, matters that keep coming back or take too many turns, and the flaws in the Harness's own
  filing. They need no model, can be recomputed for any past day, and are what a correction is judged by.
* **The audit** (`run_audit_for_date`) is one tool-less call to an independent model (the `[audit]`
  chain) that is shown the metrics, the flagged turns and the day's log, and returns findings. Every
  finding must cite turns that exist in that day's log, or it is dropped.

A finding can turn into a *calibration note*: one short, reversible rule appended to the prompt of the
chat, digest or archive stage. That is the only thing the audit changes by itself; it never touches
code, models, gates or Improvement Loop proposals (it can only suggest those). After
`VERIFY_WINDOW_DAYS` the Harness recomputes the metric the note was meant to move and keeps the note
only if it moved; otherwise the note is retired and the operator is told.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from memtrace_harness.calibration import calibration_block  # noqa: F401  (re-exported)
from memtrace_harness.memory_digest import (
    ModelCaller,
    _extract_json_object,
    _format_log,
    split_schedule_activity,
    turns_by_local_date,
)
from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

NORTH_STAR = "使用者用最少的重複與往返，讓 Harness 一次就理解並做對事。"

KINDS = ("repeat_work", "misunderstanding", "many_roundtrips", "memory_quality")
KIND_LABELS = {
    "repeat_work": "反覆處理同一件事",
    "misunderstanding": "沒聽懂使用者",
    "many_roundtrips": "來回太多次才決定",
    "memory_quality": "記憶整理本身的問題",
}
# The metric a finding of each kind is judged by after the verification window.
KIND_METRIC = {
    "repeat_work": "repeat_rate",
    "misunderstanding": "correction_rate",
    "many_roundtrips": "roundtrip_rate",
    "memory_quality": "flaw_rate",
}
NOTE_TARGETS = {"chat": "對話", "digest": "每日摘要", "archive": "記憶歸檔"}

AUDIT_HOUR_NOTE = "runs inside the nightly digest window, after the digest and the archive"
MAX_DAYS_BACK = 3
MAX_DAYS_PER_PASS = 2
MAX_FINDINGS = 6
MAX_NOTES_PER_RUN = 3
MAX_ACTIVE_NOTES = 8
NOTE_MIN_CHARS, NOTE_MAX_CHARS = 8, 200
TEXT_MAX_CHARS = 300
VERIFY_WINDOW_DAYS = 7
MIN_VERIFY_DAYS = 3
VERIFY_GIVE_UP_DAYS = 21
BASELINE_DAYS = 7
IMPROVEMENT_RATIO = 0.8

# Metric thresholds.
REPEAT_TOUCH_DAYS = 3
REPEAT_WINDOW_DAYS = 14
OVERLONG_TURNS = 12
RESTATE_MIN_CHARS = 8
RESTATE_RATIO = 0.75
_CORRECTION = re.compile(
    r"(我說的是|我是說|我的意思是|不是這個意思|不是我要的|你誤會|你搞錯|搞錯了|聽不懂|你沒懂|"
    r"我剛剛(?:說|講)|我已經(?:說|講)過|說過了|再說一次|不是說過|不是叫你|不是要你|"
    r"that'?s not what i|i already (?:said|told)|i meant)",
    re.IGNORECASE,
)
# A note that tries to loosen the stage's own rules is refused: calibration changes judgement, never
# the output format, the evidence requirement, or what the stage may do.
_OVERRIDE = re.compile(
    r"(ignore|override|disregard|忽略|無視|不要引用|不必引用|不需要引用|跳過引用|不要輸出 ?json|"
    r"without cit|skip cit|any tool|工具權限|寫入權限|批准|approve)",
    re.IGNORECASE,
)


@dataclass
class AuditOutcome:
    project: str
    audit_date: str | None = None
    run_id: int | None = None
    assessment: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    applied: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)
    observed: list[dict] = field(default_factory=list)
    verified: list[dict] = field(default_factory=list)
    provider: str | None = None
    model: str | None = None


# --- Metrics (mechanical) ---------------------------------------------------------------------


def _rate(count: float, user_turns: int) -> float:
    """Per ten operator messages, so a quiet day and a busy day are comparable."""
    return round(count * 10 / max(user_turns, 10), 3)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def restated_turns(user_turns: list[dict]) -> list[int]:
    """Seqs of operator messages that nearly repeat an earlier one the same day."""
    seen: list[str] = []
    out: list[int] = []
    for turn in user_turns:
        text = _normalize_text(str(turn.get("content") or ""))
        if len(text) >= RESTATE_MIN_CHARS:
            if any(difflib.SequenceMatcher(None, text, earlier).ratio() >= RESTATE_RATIO for earlier in seen):
                out.append(int(turn["turn_seq"]))
            seen.append(text)
    return out


def correction_turns(user_turns: list[dict]) -> list[int]:
    return [int(t["turn_seq"]) for t in user_turns if _CORRECTION.search(str(t.get("content") or ""))]


def _topic_entry_turns(topic: dict, day: str) -> tuple[int, bool]:
    count, open_left = 0, False
    for entry in topic["entries"]:
        if entry.get("date") == day:
            count += len(set(entry.get("turns") or []))
            open_left = open_left or bool(entry.get("open_items"))
    return count, open_left


@dataclass
class _MetricContext:
    """What the day metrics read from the store, fetched once for a whole range of days."""

    by_date: dict[str, list[dict]]
    topics: dict[int, dict]
    topic_days: dict[str, set[int]]
    digests: dict[str, dict]
    undone_reorg_dates: list[str]
    topic_dates_by_id: dict[int, set[str]]


def build_metric_context(
    trace_store: TraceStore, project: str, session_id: str, workspace_id: str, tz: tzinfo, first: str, last: str
) -> _MetricContext:
    start = (date.fromisoformat(first) - timedelta(days=REPEAT_WINDOW_DAYS)).isoformat()
    pairs = trace_store.topic_days_between(project, start, last)
    topic_days: dict[str, set[int]] = {}
    by_topic: dict[int, set[str]] = {}
    for topic_id, day in pairs:
        topic_days.setdefault(day, set()).add(topic_id)
        by_topic.setdefault(topic_id, set()).add(day)
    topics = {t["id"]: t for t in trace_store.list_memory_topics(project, workspace_id)}
    undone = []
    for run in trace_store.list_undone_reorg_runs(project):
        try:
            undone.append(datetime.fromisoformat(run["undone_at"]).astimezone(tz).date().isoformat())
        except (TypeError, ValueError):
            continue
    return _MetricContext(
        by_date=turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz),
        topics=topics,
        topic_days=topic_days,
        digests={d["digest_date"]: d["digest"] for d in trace_store.list_memory_digests(project)},
        undone_reorg_dates=undone,
        topic_dates_by_id=by_topic,
    )


def compute_metrics(ctx: _MetricContext, day: str) -> dict[str, Any]:
    turns = ctx.by_date.get(day, [])
    user = [t for t in turns if t["speaker"] == "user"]
    corrections = correction_turns(user)
    restated = restated_turns(user)

    window_start = (date.fromisoformat(day) - timedelta(days=REPEAT_WINDOW_DAYS - 1)).isoformat()
    repeated, overlong = [], []
    for topic_id in sorted(ctx.topic_days.get(day, ())):
        topic = ctx.topics.get(topic_id)
        if topic is None:
            continue
        touched = {d for d in ctx.topic_dates_by_id.get(topic_id, ()) if window_start <= d <= day}
        if len(touched) >= REPEAT_TOUCH_DAYS:
            repeated.append(topic["title"])
        turn_count, open_left = _topic_entry_turns(topic, day)
        if turn_count >= OVERLONG_TURNS and open_left:
            overlong.append(topic["title"])

    digest = ctx.digests.get(day) or {}
    discarded = int(digest.get("discarded_ungrounded") or 0)
    needs_operator = sum(1 for i in digest.get("open_items") or [] if i.get("needs_operator"))
    undone = sum(1 for d in ctx.undone_reorg_dates if d == day)
    flaws = discarded + needs_operator + undone

    n = len(user)
    metrics = {
        "user_turns": n,
        "correction_turns": corrections,
        "restated_turns": restated,
        "repeated_matters": repeated,
        "overlong_matters": overlong,
        "discarded_ungrounded": discarded,
        "needs_operator": needs_operator,
        "undone_reorgs": undone,
        "correction_rate": _rate(len(corrections), n),
        "repeat_rate": _rate(len(restated) + len(repeated), n),
        "roundtrip_rate": _rate(len(overlong), n),
        "flaw_rate": _rate(flaws, n),
    }
    metrics["friction"] = round(
        metrics["correction_rate"] + metrics["repeat_rate"] + metrics["roundtrip_rate"] + metrics["flaw_rate"], 3
    )
    return metrics


def mean_metric(ctx: _MetricContext, key: str, days: list[str]) -> float | None:
    """Mean of one metric over the days that had operator messages; None when none did."""
    values = [m[key] for m in (compute_metrics(ctx, d) for d in days) if m["user_turns"] > 0]
    return round(sum(values) / len(values), 3) if values else None


def baseline_days(ctx: _MetricContext, day: str) -> list[str]:
    earlier = sorted(d for d in ctx.by_date if d < day and d >= (date.fromisoformat(day) - timedelta(days=14)).isoformat())
    return earlier[-BASELINE_DAYS:]


# --- Calibration notes ------------------------------------------------------------------------


def check_note(trace_store: TraceStore, project: str, target: str, text: str) -> str | None:
    """None when a note may be adopted, otherwise why not."""
    if target not in NOTE_TARGETS:
        return "unknown target"
    if not NOTE_MIN_CHARS <= len(text) <= NOTE_MAX_CHARS:
        return "length out of range"
    if _OVERRIDE.search(text):
        return "tries to loosen the stage's rules"
    active = trace_store.list_calibration_notes(project, target=target)
    if len(active) >= MAX_ACTIVE_NOTES:
        return "too many active notes for this stage"
    norm = _normalize_text(text)
    if any(difflib.SequenceMatcher(None, norm, _normalize_text(n["text"])).ratio() >= 0.85 for n in active):
        return "duplicates an active note"
    return None


# --- The audit prompt and its grounding --------------------------------------------------------


def _findings_history(findings: list[dict]) -> str:
    if not findings:
        return "(none yet)"
    lines = []
    for f in findings:
        verdict = ""
        if f.get("outcome"):
            o = f["outcome"]
            verdict = f" → {o.get('verdict')} (before {o.get('baseline')}, after {o.get('after')})"
        lines.append(f"- [{f['audit_date']}] {f['kind']} / {f['status']}: {f['text']}{verdict}")
    return "\n".join(lines)


def build_audit_prompt(
    project: str,
    day: str,
    shown_turns: list[dict],
    metrics: dict[str, Any],
    baseline: dict[str, float | None],
    active_notes: list[dict],
    history: list[dict],
    schedule_note: str = "",
) -> str:
    notes = "\n".join(f"- ({n['target']}) {n['text']}" for n in active_notes) or "(none)"
    schedule = f"\nScheduled runs that day (not conversation):\n{schedule_note}\n" if schedule_note else ""
    kinds = ", ".join(KINDS)
    return f"""You are an independent auditor of the Harness, the assistant that works with the operator of the project "{project}". You are NOT the assistant and you do not defend it. Judge only from evidence in the log and the numbers below.

NORTH STAR: {NORTH_STAR}
Ask of {day}: where did the operator have to repeat themselves, explain again, correct the assistant, or go back and forth many times before something was settled? Was anything the Harness filed into memory wrong, duplicated, too fragmented, or missing so that it will cost the operator effort later? If the day went well, say so and report nothing: an empty findings list is a correct answer. Do not invent problems, do not praise, do not pad.

Numbers for {day} (mechanical, computed by the Harness; per-ten-messages rates) and the operator's usual level over the previous days:
{_metrics_text(metrics, baseline)}

Calibration notes already in force (do not repeat them; a note that is in force and still being violated is itself a finding):
{notes}

Earlier findings and what became of them (learn from what did not work; do not propose the same fix again):
{_findings_history(history)}
{schedule}
Rules:
1. A finding is {{"kind": one of [{kinds}], "text": what went wrong, concretely, "turns": [n, ...], "action": ...}}. It MUST cite the turn numbers (#n) that show it, only turns in the log below. A finding without valid turns is discarded (memory_quality may cite none when the numbers show it).
2. "action" is one of:
   - {{"type": "note", "target": "chat" | "digest" | "archive", "text": "<one short rule>"}}: a calibration note put into that stage's prompt from tomorrow. chat = the assistant talking to the operator; digest = the nightly summary of the day; archive = the split of the day into matters. The text is ONE imperative sentence in Traditional Chinese, at most {NOTE_MAX_CHARS} characters, general enough to apply to future days (a habit or a misreading), not a one-off fact. It may change how the stage judges; it may not change the output format, drop the need to cite turns, or grant any permission.
   - {{"type": "proposal", "text": "..."}}: something only the operator can approve (a prompt rewrite, a model change, a process change, a new tool).
   - {{"type": "none"}}: worth knowing, nothing to change.
3. At most {MAX_FINDINGS} findings, at most {MAX_NOTES_PER_RUN} notes. Prefer the few that cost the operator the most.
4. "assessment": two or three sentences, in Traditional Chinese, judging the day against the north star. State facts and numbers, not feelings.

Reply with ONE JSON object and nothing else:
{{"assessment": "...", "findings": [{{"kind": "...", "text": "...", "turns": [1], "action": {{"type": "none"}}}}]}}

=== Conversation log of {day} ===
{_format_log(shown_turns)}
"""


def _metrics_text(metrics: dict[str, Any], baseline: dict[str, float | None]) -> str:
    def usual(key: str) -> str:
        value = baseline.get(key)
        return "n/a" if value is None else f"{value}"

    lines = [
        f"operator messages: {metrics['user_turns']}",
        f"friction (sum of the four rates below): {metrics['friction']}  (usual {usual('friction')})",
        f"correction_rate (operator corrected / re-explained to the assistant): {metrics['correction_rate']}  (usual {usual('correction_rate')}); turns {metrics['correction_turns'] or '-'}",
        f"repeat_rate (restated messages + matters that keep coming back): {metrics['repeat_rate']}  (usual {usual('repeat_rate')}); restated turns {metrics['restated_turns'] or '-'}; recurring matters {metrics['repeated_matters'] or '-'}",
        f"roundtrip_rate (matters that took many turns and are still open): {metrics['roundtrip_rate']}  (usual {usual('roundtrip_rate')}); {metrics['overlong_matters'] or '-'}",
        f"flaw_rate (digest items dropped as ungrounded, questions the digest could not settle, undone reorganisations): {metrics['flaw_rate']}  (usual {usual('flaw_rate')})",
    ]
    return "\n".join(lines)


def _as_seq(value: Any) -> int | None:
    try:
        return int(str(value).strip().lstrip("#"))
    except ValueError:
        return None


def ground_audit(raw: dict[str, Any], *, valid_seqs: set[int]) -> tuple[str, list[dict], int]:
    """Check the model's answer against that day's log. Returns (assessment, findings, dropped)."""
    assessment = re.sub(r"\s+", " ", str(raw.get("assessment") or "")).strip()[:TEXT_MAX_CHARS * 2]
    findings: list[dict] = []
    dropped = 0
    items = raw.get("findings")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(findings) >= MAX_FINDINGS:
            dropped += 1
            continue
        kind = item.get("kind")
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()[:TEXT_MAX_CHARS]
        cited = item.get("turns")
        turns = sorted({n for n in map(_as_seq, cited) if n in valid_seqs}) if isinstance(cited, list) else []
        if kind not in KINDS or not text or (not turns and kind != "memory_quality"):
            dropped += 1
            continue
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        kind_of_action = action.get("type")
        clean: dict[str, Any] = {"type": "none"}
        if kind_of_action == "note" and action.get("target") in NOTE_TARGETS:
            note = re.sub(r"\s+", " ", str(action.get("text") or "")).strip()
            if note:
                clean = {"type": "note", "target": action["target"], "text": note}
        elif kind_of_action == "proposal":
            proposal = re.sub(r"\s+", " ", str(action.get("text") or "")).strip()[:TEXT_MAX_CHARS]
            if proposal:
                clean = {"type": "proposal", "text": proposal}
        findings.append({"kind": kind, "text": text, "turns": turns, "action": clean})
    return assessment, findings, dropped


# --- Running ----------------------------------------------------------------------------------


def due_audit_dates(
    trace_store: TraceStore, project: str, session_id: str, tz: tzinfo, now: datetime, *, max_days_back: int | None = MAX_DAYS_BACK
) -> list[str]:
    """Finished local days with turns that were digested and archived but not yet audited,
    oldest first (a note written for one day is then in force for the next)."""
    today = now.astimezone(tz).date()
    grouped = turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz)
    digested = {d["digest_date"] for d in trace_store.list_memory_digests(project)}
    archived = trace_store.topic_run_dates(project)
    done = trace_store.audited_dates(project)
    dates = sorted(d for d in grouped if d < today.isoformat() and d in digested and d in archived and d not in done)
    if max_days_back is not None:
        earliest = (today - timedelta(days=max_days_back)).isoformat()
        dates = [d for d in dates if d >= earliest]
    return dates[:MAX_DAYS_PER_PASS]


def run_audit_for_date(
    trace_store: TraceStore,
    project: str,
    session_id: str,
    workspace_id: str,
    day: str,
    tz: tzinfo,
    call_model: ModelCaller,
) -> AuditOutcome:
    ctx = build_metric_context(trace_store, project, session_id, workspace_id, tz, day, day)
    turns = ctx.by_date.get(day, [])
    if not turns:
        raise ValueError(f"no turns for {project} on {day}")
    metrics = compute_metrics(ctx, day)
    shown, conversation_turns, schedule_note = split_schedule_activity(turns)
    base_days = baseline_days(ctx, day)
    baseline = {k: mean_metric(ctx, k, base_days) for k in (*KIND_METRIC.values(), "friction")}
    outcome = AuditOutcome(project=project, audit_date=day, metrics=metrics)

    if not conversation_turns or metrics["user_turns"] == 0:
        outcome.run_id = trace_store.save_audit_run(
            project=project, audit_date=day, provider=None, model=None, metrics=metrics, assessment=""
        )
        return outcome

    active = trace_store.list_calibration_notes(project)
    history = trace_store.list_audit_findings(project, limit=12)
    stdout, provider, model = call_model(
        build_audit_prompt(project, day, shown, metrics, baseline, active, history, schedule_note)
    )
    assessment, findings, dropped = ground_audit(
        _extract_json_object(stdout), valid_seqs={int(t["turn_seq"]) for t in shown}
    )
    if dropped:
        logger.info(f"[{project}] {day}: {dropped} ungrounded audit finding(s) dropped")
    metrics["discarded_findings"] = dropped
    outcome.assessment, outcome.provider, outcome.model = assessment, provider, model
    outcome.run_id = trace_store.save_audit_run(
        project=project, audit_date=day, provider=provider, model=model, metrics=metrics, assessment=assessment
    )
    verify_after = (date.fromisoformat(day) + timedelta(days=VERIFY_WINDOW_DAYS + 1)).isoformat()
    notes_made = 0
    for finding in findings:
        action = finding["action"]
        status, note_id, why_not = "observed", None, None
        if action["type"] == "note":
            why_not = None if notes_made < MAX_NOTES_PER_RUN else "note limit for one night"
            why_not = why_not or check_note(trace_store, project, action["target"], action["text"])
            if why_not is None:
                status = "applied"
            else:
                status = "proposed"
                logger.info(f"[{project}] audit note not adopted ({why_not}): {action['text']}")
        elif action["type"] == "proposal":
            status = "proposed"
        baseline_value = baseline.get(KIND_METRIC[finding["kind"]]) if status == "applied" else None
        finding_id = trace_store.add_audit_finding(
            run_id=outcome.run_id, project=project, audit_date=day, kind=finding["kind"], text=finding["text"],
            turns=finding["turns"], action=action["type"], note_id=None, baseline=baseline_value, status=status,
            verify_after=verify_after if status == "applied" else None,
        )
        record = {**finding, "finding_id": finding_id, "why_not": why_not}
        if status == "applied":
            note_id = trace_store.add_calibration_note(
                project=project, target=action["target"], text=action["text"], finding_id=finding_id
            )
            trace_store.set_finding_note(finding_id, note_id)
            notes_made += 1
            outcome.applied.append({**record, "note_id": note_id})
        elif status == "proposed":
            outcome.proposals.append({**record, "text_for_operator": action.get("text") or finding["text"]})
        else:
            outcome.observed.append(record)
    return outcome


def verify_findings(
    trace_store: TraceStore, project: str, session_id: str, workspace_id: str, tz: tzinfo, now: datetime
) -> list[dict]:
    """Judge each applied note by the metric it was meant to move, once its window has passed.
    A note that did not help is retired. Returns one record per finding settled this time."""
    today = now.astimezone(tz).date().isoformat()
    due = [
        f for f in trace_store.list_audit_findings(project, statuses=("applied",), limit=200)
        if f["verify_after"] and f["verify_after"] <= today
    ]
    if not due:
        return []
    first = min(f["audit_date"] for f in due)
    ctx = build_metric_context(trace_store, project, session_id, workspace_id, tz, first, today)
    settled: list[dict] = []
    for f in sorted(due, key=lambda f: f["id"]):
        key = KIND_METRIC[f["kind"]]
        after_days = sorted(d for d in ctx.by_date if d > f["audit_date"] and d < today)[:VERIFY_WINDOW_DAYS]
        active_days = [d for d in after_days if compute_metrics(ctx, d)["user_turns"] > 0]
        if len(active_days) < MIN_VERIFY_DAYS:
            if (date.fromisoformat(today) - date.fromisoformat(f["audit_date"])).days <= VERIFY_GIVE_UP_DAYS:
                continue
            verdict, after = "inconclusive", None
        else:
            after = mean_metric(ctx, key, active_days)
            base = f["baseline"]
            if base is None or base == 0:
                verdict = "inconclusive"
            elif after is not None and after <= base * IMPROVEMENT_RATIO:
                verdict = "effective"
            elif after is not None and after > base * 1.2:
                verdict = "worse"
            else:
                verdict = "no_change"
        outcome = {"verdict": verdict, "metric": key, "baseline": f["baseline"], "after": after, "days": len(active_days)}
        note = next((n for n in trace_store.list_calibration_notes(project, active_only=False) if n["id"] == f["note_id"]), None)
        if verdict in ("worse", "no_change"):
            status = "ineffective"
            if note and note["active"]:
                trace_store.set_calibration_note_active(note["id"], False, f"audit: {verdict}")
        else:
            status = "effective" if verdict == "effective" else "inconclusive"
        trace_store.set_audit_finding_status(f["id"], status, outcome)
        settled.append({"finding": f, "note": note, "status": status, **outcome})
    return settled


def run_audit_pass(
    trace_store: TraceStore,
    project: str,
    session_id: str,
    workspace_id: str,
    tz: tzinfo,
    call_model: ModelCaller,
    now: datetime,
    *,
    max_days_back: int | None = MAX_DAYS_BACK,
    only_date: str | None = None,
) -> list[AuditOutcome]:
    """One night's audit for one project: settle the notes that are due, then audit the days
    not yet audited. Stops at the first failed day so notes are written in order."""
    outcomes: list[AuditOutcome] = []
    settled = verify_findings(trace_store, project, session_id, workspace_id, tz, now)
    if settled:
        outcomes.append(AuditOutcome(project=project, verified=settled))
    dates = [only_date] if only_date else due_audit_dates(trace_store, project, session_id, tz, now, max_days_back=max_days_back)
    for day in dates:
        outcomes.append(run_audit_for_date(trace_store, project, session_id, workspace_id, day, tz, call_model))
    return outcomes


# --- Undo and reporting -----------------------------------------------------------------------


def undo_audit_run(trace_store: TraceStore, run_id: int) -> int:
    """Retire every note the run adopted. Returns how many. Findings stay on record."""
    run = trace_store.get_audit_run(run_id)
    if run is None or run["undone_at"]:
        return 0
    count = 0
    for f in trace_store.list_audit_findings(run["project"], run_id=run_id, limit=200):
        if f["status"] == "applied" and f["note_id"]:
            trace_store.set_calibration_note_active(f["note_id"], False, "undone by operator")
            trace_store.set_audit_finding_status(f["id"], "undone")
            count += 1
    trace_store.mark_audit_run_undone(run_id)
    return count


def describe_audit(outcome: AuditOutcome) -> str:
    lines: list[str] = []
    if outcome.audit_date:
        m = outcome.metrics
        head = f"🔍 夜間稽核 {outcome.audit_date}（{outcome.project}）"
        lines.append(head)
        if outcome.assessment:
            lines.append(outcome.assessment)
        if m.get("user_turns"):
            lines.append(
                f"摩擦指數 {m['friction']}（糾正 {len(m['correction_turns'])}、重述 {len(m['restated_turns'])}、"
                f"反覆事項 {len(m['repeated_matters'])}、冗長未決 {len(m['overlong_matters'])}）"
            )
        for item in outcome.applied:
            lines.append(f"✅ 已校正（{NOTE_TARGETS[item['action']['target']]}）：{item['action']['text']}\n   起因：{KIND_LABELS[item['kind']]}——{item['text']}")
        for item in outcome.proposals:
            lines.append(f"💡 建議（需你決定）：{item['text_for_operator']}")
        for item in outcome.observed:
            lines.append(f"👁 觀察：{KIND_LABELS[item['kind']]}——{item['text']}")
        if not (outcome.applied or outcome.proposals or outcome.observed):
            lines.append("沒有發現需要校正的地方。")
    for v in outcome.verified:
        note = v.get("note")
        text = note["text"] if note else v["finding"]["text"]
        label = {
            "effective": "📉 校正有效",
            "ineffective": "↩️ 校正沒有改善，已撤回",
            "inconclusive": "❔ 校正無法判定",
        }[v["status"]]
        detail = "" if v["after"] is None else f"（{v['metric']} {v['baseline']} → {v['after']}）"
        lines.append(f"{label}{detail}：{text}")
    return "\n".join(lines)


def audit_status(trace_store: TraceStore, project: str) -> dict[str, Any]:
    """What the status page shows: the north star, the friction trend, the notes in force."""
    runs = trace_store.list_audit_runs(project, limit=14)
    return {
        "north_star": NORTH_STAR,
        "trend": [
            {"date": r["audit_date"], "friction": r["metrics"].get("friction"), "user_turns": r["metrics"].get("user_turns")}
            for r in reversed(runs)
        ],
        "latest_assessment": next((r["assessment"] for r in runs if r["assessment"]), ""),
        "notes": [
            {"id": n["id"], "target": n["target"], "text": n["text"], "since": n["created_at"]}
            for n in trace_store.list_calibration_notes(project)
        ],
        "open_proposals": [
            {"id": f["id"], "date": f["audit_date"], "kind": f["kind"], "text": f["text"]}
            for f in trace_store.list_audit_findings(project, statuses=("proposed",), limit=10)
        ],
    }
