"""The Controller reads what an unattended trigger produced before it reaches the operator.

A schedule run and a scanner pass both used to go straight to the human: every occurrence of a
schedule pushed its result, and the scanner turned the oldest backlog item into an approval
request without anyone having looked at it. Both now pass through one tool-less Controller
judgment first (same provider/model as the project's Controller role, run in the same neutral
sandbox), which may only *reduce* what the operator is shown, and only on grounds the harness
can check:

* schedule results — push, hold for the nightly digest, or pause a schedule that no longer
  has a reason to run (and say so, with a one-tap way back). Holding a result back must cite
  which earlier report it repeats; errors are never held back; a run of silent results is cut
  short by a periodic push; and any trouble with the review itself means the result is pushed
  exactly as before.
* scanner candidates — propose it, defer it a bounded number of times, or drop it as stale
  citing an earlier decision of the operator's; anything else the review says, or fails to
  say, leaves the candidate proposed as before.

The model never gains a way to act: it returns a verdict, and the harness decides whether the
verdict is admissible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from memtrace_harness.decision_records import known_basis_ids

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig

logger = logging.getLogger(__name__)

REVIEW_TIMEOUT_SECONDS = 120
MAX_HISTORY = 5
# After this many results in a row held back, the next one is pushed regardless: silence must
# never be able to last indefinitely just because the model keeps judging "nothing new".
MAX_CONSECUTIVE_SILENT = 12
# A schedule needs at least this much history before the Controller may judge it no longer
# needed; one or two reports are not a pattern.
MIN_HISTORY_TO_PAUSE = 3
# A scanner candidate can be deferred at most this many times before it is proposed anyway.
MAX_SCANNER_DEFERRALS = 3
MAX_CANDIDATES_REVIEWED = 5

SILENT_TAG = "【未推送"

ReviewCaller = Callable[[str], str | None]

DELIVERIES = ("push", "digest_only", "pause_and_notify")
SCANNER_VERDICTS = ("propose", "defer", "drop")

_HELD_BACK_STATUSES = frozenset({"succeeded", "needs_human"})


@dataclass(frozen=True)
class ScheduleDelivery:
    action: str
    reason: str
    # Why the harness overrode (or never asked) the model, when it did; None for a verdict
    # the model gave and the harness accepted.
    overridden: str | None = None


@dataclass(frozen=True)
class ScannerVerdict:
    verdict: str
    reason: str
    evidence: tuple[str, ...] = ()


def make_review_caller(config: HarnessConfig, project_name: str) -> ReviewCaller:
    """One call through the project's Controller role: its own model first, then its own
    fallbacks, in the Controller's neutral workspace. Returns the model's stdout or None."""
    from memtrace_harness.cli_process import CliProcessRunner
    from memtrace_harness.role_profiles import load_role_profiles

    def call(prompt: str) -> str | None:
        profile = load_role_profiles(config.role_profiles_file_for(project_name))["controller"]
        sandbox = (config.trace_root / "controller-workspace").resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        for candidate in [profile, *profile.fallbacks]:
            cmd = config.chat_command(candidate.provider, candidate.model, prompt)
            result = CliProcessRunner().run(cmd, cwd=sandbox, timeout_seconds=REVIEW_TIMEOUT_SECONDS)
            if result.return_code == 0 and result.stdout.strip():
                return result.stdout
        return None

    return call


def _parse(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    from memtrace_harness.loop import parse_json_object

    return parse_json_object(raw)


def _clip(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---- schedule results ---------------------------------------------------------------------


def history_entry(turn_content: str) -> dict[str, Any]:
    """A past schedule_report turn as the review sees it. Reports written before holding back
    existed carry no tag and were all pushed."""
    return {"text": turn_content, "pushed": not turn_content.startswith(SILENT_TAG)}


def silent_tag(reason: str) -> str:
    return f"{SILENT_TAG}：{_clip(reason, 80)}】"


def build_schedule_prompt(
    *,
    schedule_id: str,
    schedule_text: str,
    goal: str,
    local_time: str,
    status: str,
    result: str,
    history: list[dict[str, Any]],
    precedent: str,
    last_operator_message: str | None,
) -> str:
    previous = (
        "\n".join(
            f"R{i}. [{'pushed to the operator' if h['pushed'] else 'held back'}] "
            f"{_clip(h['text'], 600)}"
            for i, h in enumerate(history, start=1)
        )
        or "(no earlier report: this is the first result of this schedule)"
    )
    return (
        "You are the Controller of an agent harness. An unattended scheduled run has just "
        "finished and its result is about to be pushed to the human operator's phone. Decide "
        "whether the operator should be shown it. A result that repeats the last one, or reports "
        "nothing new, only trains the operator to stop reading; a result that matters must never "
        "be held back. You have no tools: judge only from what is below.\n\n"
        f"Schedule {schedule_id}: {schedule_text}\n"
        f"Its task: {goal}\n"
        f"Now: {local_time}. Operator's last message in this project: "
        f"{last_operator_message or 'unknown'}.\n\n"
        f"Earlier reports of this schedule, newest first (R1 is the most recent):\n{previous}\n\n"
        f"NEW RESULT (loop status: {status}):\n{_clip(result, 3000)}\n\n"
        + (f"{precedent}\n\n" if precedent else "")
        + "Answer with ONLY one JSON object:\n"
        '{"delivery": "push" | "digest_only" | "pause_and_notify", "reason": "<one sentence in '
        'Traditional Chinese>", "compared_to": <number of the earlier report R<n> this result '
        "repeats or adds nothing to, or null>}\n"
        "- push: the operator should see it now. When unsure, push.\n"
        "- digest_only: nothing here the operator needs now; it is recorded and appears in the "
        "nightly digest. You MUST give compared_to (the R<n> it repeats); without it the result is "
        "pushed anyway. If the new result's loop status is needs_human, compared_to must be an "
        "earlier report that WAS pushed and raised the same concern.\n"
        "- pause_and_notify: the schedule itself no longer has a reason to run (what it watches no "
        "longer exists or applies, and earlier reports already showed that). The operator is told "
        "and can resume with one tap. Use it only with a clear reason in the reports above.\n"
        "A past decision of the operator's marked with a warning sign went against a recommendation "
        "like yours; weigh it. Never decide from the operator's silence alone."
    )


def decide_schedule_delivery(
    *,
    caller: ReviewCaller,
    status: str,
    history: list[dict[str, Any]],
    prompt: str,
) -> ScheduleDelivery:
    """The delivery for one schedule result. Every path that is not a clean, admissible verdict
    ends in a push, so the review can only ever remove noise, never an alert. `history` is the
    schedule's earlier reports newest first (as many as the streak check needs); the prompt shows
    only the first MAX_HISTORY of them, and those are the only ones a verdict may cite."""
    shown = history[:MAX_HISTORY]
    if status not in _HELD_BACK_STATUSES:
        return ScheduleDelivery("push", "run did not finish cleanly", overridden="error")
    silent_streak = 0
    for entry in history:
        if entry["pushed"]:
            break
        silent_streak += 1
    if silent_streak >= MAX_CONSECUTIVE_SILENT:
        return ScheduleDelivery(
            "push", f"{silent_streak} results in a row were held back", overridden="heartbeat"
        )
    try:
        verdict = _parse(caller(prompt))
    except Exception:
        logger.exception("schedule delivery review failed; pushing the result")
        return ScheduleDelivery("push", "review unavailable", overridden="review_unavailable")
    if not isinstance(verdict, dict) or verdict.get("delivery") not in DELIVERIES:
        return ScheduleDelivery("push", "review gave no usable verdict", overridden="review_unavailable")
    action = verdict["delivery"]
    reason = _clip(verdict.get("reason") or "", 200)
    if action == "push":
        return ScheduleDelivery("push", reason or "pushed")
    if not reason:
        return ScheduleDelivery("push", "review gave no reason", overridden="no_reason")
    if action == "digest_only":
        compared = verdict.get("compared_to")
        if isinstance(compared, bool) or not isinstance(compared, int) or not 1 <= compared <= len(shown):
            return ScheduleDelivery("push", reason, overridden="no_comparison_cited")
        if status == "needs_human" and not shown[compared - 1]["pushed"]:
            return ScheduleDelivery("push", reason, overridden="repeats_a_report_the_operator_never_saw")
        return ScheduleDelivery("digest_only", reason)
    # pause_and_notify
    if status == "needs_human":
        return ScheduleDelivery("push", reason, overridden="alert_not_paused")
    if len(history) < MIN_HISTORY_TO_PAUSE:
        return ScheduleDelivery("push", reason, overridden="too_little_history_to_pause")
    return ScheduleDelivery("pause_and_notify", reason)


# ---- scanner candidates -------------------------------------------------------------------


def build_scanner_prompt(
    *,
    candidates: list[dict[str, Any]],
    precedent: str,
    deferrals: dict[str, int],
) -> str:
    listed = "\n\n".join(
        f"[{c['id']}] {_clip(c.get('title') or c['id'], 120)}\n"
        f"created {str(c.get('created_at') or '?')[:10]}"
        + (f", already deferred {deferrals[c['id']]}x" if deferrals.get(c["id"]) else "")
        + f"\n{_clip(c.get('body') or '(no body)', 1200)}"
        for c in candidates
    )
    return (
        "You are the Controller of an agent harness. An unattended scan found backlog items that "
        "look ready to build. Before the harness asks the human operator to approve one, read "
        "them. You have no tools: judge only from what is below.\n\n"
        f"Candidates, oldest first:\n{listed}\n\n"
        + (f"{precedent}\n\n" if precedent else "")
        + "Answer with ONLY one JSON object, one verdict per candidate:\n"
        '{"verdicts": [{"id": "<candidate id>", "verdict": "propose" | "defer" | "drop", '
        '"reason": "<one sentence in Traditional Chinese: why the operator should be asked now / '
        'why not yet / why it no longer applies>", "evidence": ["D<n>", ...]}]}\n'
        "- propose: worth asking the operator about now. The reason is shown to them, so say what "
        "it changes and anything that makes it risky. The harness asks about the first proposed "
        "candidate only. When unsure, propose.\n"
        "- defer: not now (it plainly depends on or conflicts with something in progress). A "
        "candidate can only be deferred a few times.\n"
        "- drop: it no longer applies because of a decision the operator already made. You MUST "
        "list in evidence the D<n> ids of those decisions from the operator precedent above; a "
        "drop without them is ignored and the candidate is proposed."
    )


def decide_scanner_verdicts(
    *,
    caller: ReviewCaller,
    candidates: list[dict[str, Any]],
    prompt: str,
    precedent: str,
    deferrals: dict[str, int],
) -> dict[str, ScannerVerdict]:
    """A verdict per candidate id. A candidate the model did not mention, or whose verdict the
    harness cannot admit, is proposed — the unreviewed behaviour — and a total failure of the
    review is an empty dict."""
    try:
        parsed = _parse(caller(prompt))
    except Exception:
        logger.exception("scanner candidate review failed; proposing as before")
        return {}
    raw = parsed.get("verdicts") if isinstance(parsed, dict) else None
    if not isinstance(raw, list):
        return {}
    known_ids = {c["id"] for c in candidates}
    offered = known_basis_ids(precedent)
    result: dict[str, ScannerVerdict] = {}
    for item in raw:
        if not isinstance(item, dict) or item.get("id") not in known_ids or item["id"] in result:
            continue
        verdict = item.get("verdict")
        reason = _clip(item.get("reason") or "", 200)
        if verdict not in SCANNER_VERDICTS or not reason:
            continue
        candidate_id = item["id"]
        evidence_raw = item.get("evidence")
        evidence = tuple(
            e for e in (evidence_raw if isinstance(evidence_raw, list) else [])
            if isinstance(e, str) and e in offered and e.startswith("D")
        )
        if verdict == "drop" and not evidence:
            continue
        if verdict == "defer" and deferrals.get(candidate_id, 0) >= MAX_SCANNER_DEFERRALS:
            continue
        result[candidate_id] = ScannerVerdict(verdict, reason, evidence)
    return result
