"""Nightly "sleep cycle" for cold memory.

The hourly consolidation pass (PrimarySessionManager.consolidate_to_memtrace) only
copies raw transcript batches into MemTrace: episodes, never abstracted. Once a day,
for each project and each finished local day, this module replays that day's turns
through one tool-less model call and keeps a grounded digest:

- summary / decisions / facts / open items, injected into the next days' chat and
  Agent Loop context so mid-term continuity no longer depends on a model choosing to
  search MemTrace;
- open items carry forward deterministically until a later day cites their
  resolution, or they age out;
- process lessons, kept in the digest for the operator to read (not auto-written to
  the Improvement Loop);
- preference candidates, which never take effect on their own: they wait in
  preference_rules for the operator to adopt, edit, or dismiss on the status page.

Grounding is enforced here, not trusted to the model: every item must cite turn
numbers that exist in that day's log, and a preference candidate may only cite the
human's own turns. Anything else is discarded and counted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

from memtrace_harness.primary_session import SCHEDULE_REPORT, SCHEDULE_TRIGGER
from memtrace_harness.trace_store import TraceStore

logger = logging.getLogger(__name__)

OPERATOR_PROFILE_TITLE = "Operator preference profile"
DIGEST_TITLE_PREFIX = "Daily digest"

# The nightly pass only runs inside this local hour (02:00-02:59 in
# HARNESS_SCHEDULE_TIMEZONE, Asia/Taipei by default), never "any time after it": a
# gateway restart in the afternoon must not start digesting on the spot. A night the
# gateway was down is caught up the next night (AUTO_CATCH_UP_DAYS).
DIGEST_HOUR = 2
# How far back the automatic nightly pass catches up on missed days. Older history is
# only digested by an explicit `memory-digest --backfill`, since each day costs a
# model call.
AUTO_CATCH_UP_DAYS = 2
OPEN_ITEM_MAX_AGE_DAYS = 14
REVIEW_STATUSES = ("decided", "stale", "needs_operator")
RECENT_DIGESTS_FOR_CONTEXT = 3
DIGEST_CONTEXT_CHAR_BUDGET = 6000
# Preferences are adopted by the Harness itself (nothing waits for the operator), so the
# damage one bad night can do is bounded: past this many new rules per project-day the
# rest wait on the status page instead, and only this many rules are put in front of
# the chat model, newest first.
MAX_AUTO_ADOPT_PER_PROJECT_DAY = 5
MAX_PREFERENCES_IN_CONTEXT = 40
ADOPTED_BY_OPERATOR = "operator"
ADOPTED_BY_DIGEST = "harness-digest"
ADOPTED_BY_CHAT = "harness-chat"

# Per-turn caps keep one busy day's prompt bounded; the human's own words get more
# room than long assistant replies and Agent Loop reports.
_USER_TURN_CHAR_CAP = 2000
_OTHER_TURN_CHAR_CAP = 1200
_PROMPT_LOG_CHAR_BUDGET = 120_000
_SQUEEZED_OTHER_TURN_CHAR_CAP = 400
# Used while the whole day fits the budget: effectively no truncation.
_FULL_TURN_CHAR_CAP = 20_000

PREFERENCE_CATEGORIES = (
    "language_format",
    "communication",
    "approval",
    "tools_permissions",
    "workflow",
    "other",
)
PREFERENCE_CATEGORY_LABELS = {
    "language_format": "語言與格式",
    "communication": "溝通風格",
    "approval": "核准習慣",
    "tools_permissions": "工具與權限",
    "workflow": "工作流程",
    "other": "其他",
}

# prompt -> (stdout, provider, model). Raises DigestError when no candidate answered.
ModelCaller = Callable[[str], tuple[str, str | None, str | None]]


class DigestError(RuntimeError):
    pass


@dataclass
class DigestOutcome:
    project: str
    digest_date: str
    turn_count: int
    digest: dict[str, Any]
    preferences: PreferenceChanges
    provider: str | None = None
    model: str | None = None


@dataclass
class PreferenceChanges:
    """What one digest did to the operator's standing preferences."""

    adopted: list[dict] = field(default_factory=list)
    retired: list[dict] = field(default_factory=list)
    waiting: list[dict] = field(default_factory=list)  # over the per-day cap


@dataclass
class _Grounded:
    items: list[dict[str, Any]] = field(default_factory=list)
    dropped: int = 0


def in_digest_window(now_local: datetime) -> bool:
    return now_local.hour == DIGEST_HOUR


def digest_title(project: str, digest_date: str) -> str:
    return f"{DIGEST_TITLE_PREFIX}: {project} {digest_date}"


def local_date_of(created_at: str, tz: tzinfo) -> str:
    return datetime.fromisoformat(created_at).astimezone(tz).date().isoformat()


def turns_by_local_date(turns: list[dict], tz: tzinfo) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for turn in turns:
        grouped.setdefault(local_date_of(turn["created_at"], tz), []).append(turn)
    return grouped


def due_digest_dates(
    trace_store: TraceStore,
    project: str,
    session_id: str,
    tz: tzinfo,
    now: datetime,
    *,
    max_days_back: int | None,
) -> list[str]:
    """Finished local days (before today) that have turns but no digest yet, oldest
    first so each day can carry the previous day's open items forward."""
    today = now.astimezone(tz).date()
    grouped = turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz)
    done = {d["digest_date"] for d in trace_store.list_memory_digests(project)}
    dates = sorted(d for d in grouped if d < today.isoformat() and d not in done)
    if max_days_back is not None:
        earliest = (today - timedelta(days=max_days_back)).isoformat()
        dates = [d for d in dates if d >= earliest]
    return dates


def _previous_digest(trace_store: TraceStore, project: str, digest_date: str) -> dict | None:
    for row in trace_store.list_memory_digests(project):
        if row["digest_date"] < digest_date:
            return row
    return None


def _format_turn(turn: dict, cap: int) -> str:
    content = str(turn["content"]).strip()
    if len(content) > cap:
        content = content[:cap] + "…（截斷）"
    return f"#{turn['turn_seq']} [{turn['speaker']}] {content}"


def _format_log(turns: list[dict]) -> str:
    """The whole day, verbatim, whenever it fits the prompt budget; per-turn caps only
    come in as a fallback so one busy day cannot blow the prompt up."""
    tiers = (
        (_FULL_TURN_CHAR_CAP, _FULL_TURN_CHAR_CAP),
        (_USER_TURN_CHAR_CAP, _OTHER_TURN_CHAR_CAP),
        (_USER_TURN_CHAR_CAP, _SQUEEZED_OTHER_TURN_CHAR_CAP),
    )
    for user_cap, other_cap in tiers:
        lines = [
            _format_turn(t, user_cap if t["speaker"] == "user" else other_cap) for t in turns
        ]
        if sum(len(line) for line in lines) <= _PROMPT_LOG_CHAR_BUDGET:
            break
    return "\n".join(lines)


def split_schedule_activity(turns: list[dict]) -> tuple[list[dict], int, str]:
    """A schedule that fires every few minutes would otherwise be most of a day's log.
    Keep every conversation turn, but of the schedule activity only each schedule's
    LAST result of the day (so it can still be cited), plus a one-line count note.
    Returns (turns to show, number of conversation turns, note for the prompt)."""
    conversation = [t for t in turns if t.get("turn_type") not in (SCHEDULE_TRIGGER, SCHEDULE_REPORT)]
    fired: dict[str, int] = {}
    goals: dict[str, str] = {}
    last_report: dict[str, dict] = {}
    for turn in turns:
        schedule_id = turn.get("schedule_id")
        if not schedule_id:
            continue
        if turn["turn_type"] == SCHEDULE_TRIGGER:
            fired[schedule_id] = fired.get(schedule_id, 0) + 1
            goals[schedule_id] = turn["content"].partition("觸發：")[2].strip() or goals.get(schedule_id, "")
        elif turn["turn_type"] == SCHEDULE_REPORT:
            last_report[schedule_id] = turn
    kept = sorted(conversation + list(last_report.values()), key=lambda t: int(t["turn_seq"]))
    ids = sorted(set(fired) | set(last_report))
    note = "\n".join(
        f"- {sid}「{goals.get(sid, '')[:80]}」：當天觸發 {fired.get(sid, 0)} 次"
        + ("，只列出最後一次的結果" if sid in last_report else "，沒有執行結果")
        for sid in ids
    )
    return kept, len(conversation), note


def build_digest_prompt(
    project: str,
    digest_date: str,
    turns: list[dict],
    carried_open_items: list[dict],
    schedule_note: str = "",
    adopted_rules: list[dict] | None = None,
) -> str:
    adopted_text = (
        "\n".join(f"[{r['id']}] ({'global' if r['scope'] == 'global' else 'project'}) {r['text']}" for r in adopted_rules)
        if adopted_rules
        else "(none)"
    )
    carried = (
        "\n".join(
            f"[{i}] (since {item.get('since', '?')}) {item['text']}"
            for i, item in enumerate(carried_open_items)
        )
        if carried_open_items
        else "(none)"
    )
    categories = ", ".join(PREFERENCE_CATEGORIES)
    return (
        "You are consolidating one day of a project's conversation log into long-term "
        "memory, the way sleep consolidates the day's episodes.\n"
        f"Project: {project}. Day: {digest_date}.\n"
        "Each log line is `#<turn number> [speaker] content`. Speakers: user = the human "
        "operator; assistant = the chat model; system = harness decisions; "
        "work_session_report = Agent Loop results.\n\n"
        "Open items carried over from earlier days, by index:\n"
        f"{carried}\n\n"
        "Standing preferences already adopted for this human, by id (global ones apply to "
        "every project):\n"
        f"{adopted_text}\n\n"
        + (
            "Scheduled runs that fired this day (a run repeats every few minutes, so only "
            "each schedule's LAST result is in the log below; treat it as a status "
            "snapshot, not as decisions or lessons):\n"
            f"{schedule_note}\n\n"
            if schedule_note
            else ""
        )
        +
        "Return ONLY one JSON object, no prose and no code fence, with exactly these keys:\n"
        "{\n"
        '  "summary": "3 to 8 short lines about what happened this day",\n'
        '  "decisions": [{"text": "...", "turns": [12, 15]}],\n'
        '  "facts": [{"text": "...", "turns": [13]}],\n'
        '  "new_open_items": [{"text": "...", "turns": [14]}],\n'
        '  "resolved_open_items": [{"index": 0, "turns": [16]}],\n'
        '  "open_item_review": [{"index": 1, "status": "stale", "reason": "...", "turns": [16]}],\n'
        '  "process_lessons": [{"text": "...", "turns": [17]}],\n'
        '  "preference_candidates": [{"text": "...", "turns": [18], "scope": "global", '
        f'"category": "other", "explicit": true, "replaces": []}}],\n'
        '  "preference_retirements": [{"id": 3, "turns": [19]}]\n'
        "}\n"
        "Rules:\n"
        "- A user turn may end with （回覆的訊息：「…」）. That quoted text is a message the "
        "human swiped to reply to (often a push from the harness or a script), not the "
        "human's own words: use it to understand what the reply is about, never as a "
        "preference and never as evidence of what the human said.\n"
        "- Every item must cite the turn numbers from THIS day's log that support it. "
        "Items without valid citations are discarded automatically.\n"
        "- decisions: things agreed or settled. facts: durable project knowledge worth "
        "remembering (data, results, configuration). new_open_items: questions or tasks "
        "raised and not finished this day. resolved_open_items: carried items this day's "
        "log shows were resolved, cited by their index and the resolving turns.\n"
        "- open_item_review: go through EVERY carried open item and say which of these it is. "
        "status `decided`: the log shows the human or the harness actually settled it "
        "(reason = what was decided). status `stale`: its premise no longer holds (for "
        "example the position it concerns was sold, the schedule it concerns was "
        "stopped, the failure it concerns was fixed); reason = why. status "
        "`needs_operator`: it is still real and only the human can decide or supply "
        "something; reason = the one decision or input needed. `decided` and `stale` must "
        "cite turns from THIS day's log that show it, otherwise use `needs_operator` or "
        "leave the item out. Items you leave out stay open unchanged. Do not use "
        "resolved_open_items for these; it is only for items completed in this day's log.\n"
        "- process_lessons: how the harness, models, or tools behaved (for example a "
        "model timing out or a step failing), not project content.\n"
        "- preference_candidates: ONLY standing preferences the human (speaker user) "
        "stated about how they want to be worked with in future conversations. Cite "
        "only user turns. One-off requests, questions about the current task, and "
        "project content are NOT preferences; when unsure, leave it out. scope is "
        '"global" when it applies to every project (e.g. reply language) or "project" '
        f"when only to this one. category is one of: {categories}. explicit is true only "
        "when the human stated it as a rule (e.g. 以後, 一律, 不要再, always). These are "
        "adopted automatically, so be conservative. Do not repeat an adopted preference "
        "unchanged; when a candidate refines or contradicts an adopted one, list that "
        "one's id in `replaces`.\n"
        "- preference_retirements: adopted preferences (by id) that the human, in THIS "
        "day's log, explicitly withdrew or contradicted (e.g. 不用了, 那個不要了) without "
        "giving a replacement. Cite only user turns. When unsure, leave it out.\n"
        "- Write every text field and the summary in Traditional Chinese (繁體中文，台灣用語). "
        "One sentence per item. Never invent anything that is not in the log.\n"
        "- Use [] for an empty list.\n\n"
        "Log:\n"
        f"{_format_log(turns)}\n"
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise DigestError("model output contained no JSON object")
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DigestError(f"model output was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DigestError("model output JSON was not an object")
    return parsed


def _cited_turns(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    cited = []
    for value in raw:
        try:
            cited.append(int(value))
        except (TypeError, ValueError):
            continue
    return cited


def _ground_items(raw: Any, valid_seqs: set[int]) -> _Grounded:
    grounded = _Grounded()
    if not isinstance(raw, list):
        return grounded
    for item in raw:
        text = str(item.get("text") or "").strip() if isinstance(item, dict) else ""
        turns = sorted({t for t in _cited_turns(item.get("turns")) if t in valid_seqs}) if text else []
        if not turns:
            grounded.dropped += 1
            continue
        grounded.items.append({"text": text, "turns": turns})
    return grounded


def _ids(raw: Any, valid: set[int]) -> list[int]:
    out = []
    for value in raw if isinstance(raw, list) else []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number in valid and number not in out:
            out.append(number)
    return out


def _ground_preferences(
    raw: Any, all_seqs: set[int], user_seqs: set[int], adopted_ids: set[int]
) -> _Grounded:
    """Stricter than _ground_items: every cited turn must exist AND be the human's
    own. One citation of an assistant or report turn discards the whole candidate —
    that is exactly how a stock-analysis reply once became a 'preference'. `replaces`
    keeps only ids that really are adopted preferences."""
    grounded = _Grounded()
    if not isinstance(raw, list):
        return grounded
    for item in raw:
        if not isinstance(item, dict):
            grounded.dropped += 1
            continue
        text = str(item.get("text") or "").strip()
        cited = _cited_turns(item.get("turns"))
        if not text or not cited or any(t not in all_seqs or t not in user_seqs for t in cited):
            grounded.dropped += 1
            continue
        category = str(item.get("category") or "other")
        grounded.items.append(
            {
                "text": text,
                "turns": sorted(set(cited)),
                "scope": "project" if item.get("scope") == "project" else "global",
                "category": category if category in PREFERENCE_CATEGORIES else "other",
                "explicit": item.get("explicit") is True,
                "replaces": _ids(item.get("replaces"), adopted_ids),
            }
        )
    return grounded


def _ground_retirements(raw: Any, user_seqs: set[int], adopted_ids: set[int]) -> _Grounded:
    grounded = _Grounded()
    if not isinstance(raw, list):
        return grounded
    for item in raw:
        cited = _cited_turns(item.get("turns")) if isinstance(item, dict) else []
        rule_ids = _ids([item.get("id")], adopted_ids) if isinstance(item, dict) else []
        if not rule_ids or not cited or any(t not in user_seqs for t in cited):
            grounded.dropped += 1
            continue
        grounded.items.append({"id": rule_ids[0], "turns": sorted(set(cited))})
    return grounded


def ground_digest(
    raw: dict[str, Any],
    *,
    digest_date: str,
    turns: list[dict],
    carried_open_items: list[dict],
    adopted_ids: set[int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate one model answer against that day's turns. Returns (digest, grounded
    preference candidates, grounded preference retirements)."""
    all_seqs = {int(t["turn_seq"]) for t in turns}
    user_seqs = {int(t["turn_seq"]) for t in turns if t["speaker"] == "user"}

    decisions = _ground_items(raw.get("decisions"), all_seqs)
    facts = _ground_items(raw.get("facts"), all_seqs)
    new_open = _ground_items(raw.get("new_open_items"), all_seqs)
    lessons = _ground_items(raw.get("process_lessons"), all_seqs)
    adopted = adopted_ids or set()
    preferences = _ground_preferences(raw.get("preference_candidates"), all_seqs, user_seqs, adopted)
    retirements = _ground_retirements(raw.get("preference_retirements"), user_seqs, adopted)
    dropped = (
        decisions.dropped + facts.dropped + new_open.dropped + lessons.dropped
        + preferences.dropped + retirements.dropped
    )

    resolved_turns: dict[int, list[int]] = {}
    for item in raw.get("resolved_open_items") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        turns_cited = sorted({t for t in _cited_turns(item.get("turns")) if t in all_seqs})
        if not 0 <= index < len(carried_open_items) or not turns_cited:
            dropped += 1
            continue
        resolved_turns[index] = turns_cited

    reviews: dict[int, dict[str, Any]] = {}
    for item in raw.get("open_item_review") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        status = item.get("status")
        reason = str(item.get("reason") or "").strip()
        turns_cited = sorted({t for t in _cited_turns(item.get("turns")) if t in all_seqs})
        if not 0 <= index < len(carried_open_items) or status not in REVIEW_STATUSES or not reason:
            dropped += 1
            continue
        if status in ("decided", "stale") and not turns_cited:
            # Ungrounded verdicts must not close anything: ask the human instead.
            status, turns_cited = "needs_operator", []
        reviews[index] = {"status": status, "reason": reason, "turns": turns_cited}

    day = date.fromisoformat(digest_date)
    open_items, resolved_items, expired_items = [], [], []
    for index, item in enumerate(carried_open_items):
        if index in resolved_turns:
            resolved_items.append({**item, "resolved_on": digest_date, "resolved_turns": resolved_turns[index]})
            continue
        review = reviews.get(index)
        if review and review["status"] in ("decided", "stale"):
            resolved_items.append(
                {
                    **item,
                    "resolved_on": digest_date,
                    "resolved_turns": review["turns"],
                    "resolution": review["status"],
                    "resolution_reason": review["reason"],
                }
            )
            continue
        if review and review["status"] == "needs_operator":
            item = {**item, "needs_operator": True, "operator_question": review["reason"]}
        else:
            item = {k: v for k, v in item.items() if k not in ("needs_operator", "operator_question")}
        since = item.get("since") or digest_date
        if (day - date.fromisoformat(since)).days > OPEN_ITEM_MAX_AGE_DAYS:
            expired_items.append(item)
        else:
            open_items.append(item)
    open_items.extend({**item, "since": digest_date} for item in new_open.items)

    digest = {
        "summary": str(raw.get("summary") or "").strip(),
        "decisions": decisions.items,
        "facts": facts.items,
        "open_items": open_items,
        "resolved_items": resolved_items,
        "expired_items": expired_items,
        "process_lessons": lessons.items,
        "preference_candidate_count": len(preferences.items),
        "discarded_ungrounded": dropped,
    }
    return digest, preferences.items, retirements.items


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip("。.!！").lower()


def visible_rules(rules: list[dict], project: str) -> list[dict]:
    return [r for r in rules if r["scope"] == "global" or r["project"] == project]


def apply_preference_changes(
    trace_store: TraceStore,
    project: str,
    digest_date: str,
    candidates: list[dict[str, Any]],
    retirements: list[dict[str, Any]],
    turns: list[dict],
) -> PreferenceChanges:
    """The Harness adopts and retires the operator's standing preferences on its own;
    nothing waits for a click. What keeps that safe: every change cites the human's own
    turns (checked before this runs), at most MAX_AUTO_ADOPT_PER_PROJECT_DAY rules are
    adopted per project-day, nothing is deleted (a retired rule stays on record and can
    be restored from the status page), and the human can correct any rule in chat.

    Safe to run twice for the same day: a rule already adopted is not added again, and a
    rule already retired is skipped."""
    changes = PreferenceChanges()
    content_by_seq = {int(t["turn_seq"]): str(t["content"]) for t in turns}

    def retire(rule_id: int, reason: str) -> None:
        if trace_store.update_preference_rule(
            rule_id, status="retired", expected_statuses=("adopted",), retire_reason=reason
        ):
            rule = trace_store.get_preference_rule(rule_id)
            if rule is not None:
                changes.retired.append(rule)

    for retirement in retirements:
        quote = content_by_seq.get(retirement["turns"][0], "")[:120]
        retire(retirement["id"], f"{digest_date} 你在對話中撤回（turn #{retirement['turns'][0]}）：{quote}")

    known = {
        _normalize(r["text"]) for r in trace_store.list_preference_rules(statuses=("pending", "adopted"))
    }
    adopted_today = 0
    for candidate in candidates:
        key = _normalize(candidate["text"])
        if key in known:
            continue
        known.add(key)
        evidence = [
            {"date": digest_date, "turn_seq": seq, "quote": content_by_seq[seq][:300]}
            for seq in candidate["turns"]
        ]
        auto = adopted_today < MAX_AUTO_ADOPT_PER_PROJECT_DAY
        rule_id = trace_store.add_preference_candidate(
            project=project,
            scope=candidate["scope"],
            category=candidate["category"],
            text=candidate["text"],
            evidence=evidence,
            explicit=candidate["explicit"],
            source_digest_date=digest_date,
            status="adopted" if auto else "pending",
            adopted_by=ADOPTED_BY_DIGEST if auto else None,
        )
        rule = trace_store.get_preference_rule(rule_id)
        assert rule is not None
        if not auto:
            changes.waiting.append(rule)
            continue
        adopted_today += 1
        changes.adopted.append(rule)
        for old_id in candidate["replaces"]:
            retire(old_id, f"{digest_date} 被 #{rule_id} 取代")
    return changes


def run_digest_for_date(
    trace_store: TraceStore,
    project: str,
    session_id: str,
    digest_date: str,
    tz: tzinfo,
    call_model: ModelCaller,
) -> DigestOutcome:
    turns = turns_by_local_date(trace_store.get_primary_session_turns(session_id), tz).get(digest_date, [])
    if not turns:
        raise DigestError(f"no turns for {project} on {digest_date}")
    previous = _previous_digest(trace_store, project, digest_date)
    carried = list(previous["digest"].get("open_items") or []) if previous else []

    turns, conversation_turns, schedule_note = split_schedule_activity(turns)
    adopted_rules = visible_rules(trace_store.list_preference_rules(statuses=("adopted",)), project)
    if turns:
        stdout, provider, model = call_model(
            build_digest_prompt(project, digest_date, turns, carried, schedule_note, adopted_rules)
        )
        raw = _extract_json_object(stdout)
    else:
        # Only schedule firings with no result (e.g. every run was refused by the
        # workspace lock): nothing for a model to summarise. Record the day anyway so it
        # isn't retried forever, and keep carrying the open items forward.
        provider = model = None
        raw = {"summary": "（這天只有排程觸發，沒有對話，也沒有執行結果。）"}
    digest, candidates, retirements = ground_digest(
        raw,
        digest_date=digest_date,
        turns=turns,
        carried_open_items=carried,
        adopted_ids={r["id"] for r in adopted_rules},
    )
    changes = apply_preference_changes(trace_store, project, digest_date, candidates, retirements, turns)
    digest["preferences_adopted"] = len(changes.adopted)
    digest["preferences_retired"] = len(changes.retired)
    trace_store.save_memory_digest(
        project=project,
        digest_date=digest_date,
        provider=provider,
        model=model,
        turn_count=conversation_turns,
        digest=digest,
    )
    return DigestOutcome(
        project=project,
        digest_date=digest_date,
        turn_count=conversation_turns,
        digest=digest,
        preferences=changes,
        provider=provider,
        model=model,
    )


# --- Rendering -----------------------------------------------------------------


def _bullets(items: list[dict], *, with_since: bool = False) -> list[str]:
    lines = []
    for item in items:
        suffix = f"（自 {item['since']}）" if with_since and item.get("since") else ""
        tag = f"【待你決定：{item.get('operator_question', '')}】" if item.get("needs_operator") else ""
        lines.append(f"- {item['text']}{suffix}{tag}")
    return lines


def render_digest_markdown(project: str, digest_date: str, digest: dict[str, Any]) -> str:
    """The MemTrace node body. Turn numbers stay in so a reader can go back to the
    raw transcript in the local trace store."""
    parts = [f"# {digest_title(project, digest_date)}", "", digest.get("summary") or "（無摘要）"]
    sections = [
        ("決策", digest.get("decisions") or [], False),
        ("知識與事實", digest.get("facts") or [], False),
        ("未完成事項", digest.get("open_items") or [], True),
        ("今日解決", digest.get("resolved_items") or [], True),
        ("流程教訓", digest.get("process_lessons") or [], False),
    ]
    for title, items, with_since in sections:
        if not items:
            continue
        parts += ["", f"## {title}"]
        for item in items:
            refs = item.get("turns") or item.get("resolved_turns") or []
            since = f"（自 {item['since']}）" if with_since and item.get("since") else ""
            ref_text = f" [turns {', '.join(str(r) for r in refs)}]" if refs else ""
            tag = ""
            if item.get("needs_operator"):
                tag = f"【待你決定：{item.get('operator_question', '')}】"
            elif item.get("resolution"):
                label = {"decided": "已決議", "stale": "已過時"}.get(item["resolution"], "")
                tag = f"【{label}：{item.get('resolution_reason', '')}】"
            parts.append(f"- {item['text']}{since}{tag}{ref_text}")
    parts += [
        "",
        f"_Harness nightly digest (draft). Ungrounded items discarded: "
        f"{digest.get('discarded_ungrounded', 0)}._",
    ]
    return "\n".join(parts)


def render_digests_context(digests_newest_first: list[dict], char_budget: int = DIGEST_CONTEXT_CHAR_BUDGET) -> str:
    """What chat and the Agent Loop see for the last few days. Open items only from
    the newest digest, since each digest already carries the earlier ones forward.
    Oldest days are dropped first when over budget."""
    if not digests_newest_first:
        return ""
    newest = digests_newest_first[0]
    blocks = []
    for row in reversed(digests_newest_first):
        digest = row["digest"]
        lines = [f"### {row['digest_date']}", digest.get("summary") or "（無摘要）"]
        if digest.get("decisions"):
            lines += ["決策："] + _bullets(digest["decisions"])
        if digest.get("facts"):
            lines += ["知識與事實："] + _bullets(digest["facts"])
        if digest.get("resolved_items"):
            lines += ["當天解決："] + _bullets(digest["resolved_items"])
        blocks.append("\n".join(lines))
    open_items = newest["digest"].get("open_items") or []
    tail = ("截至 " + newest["digest_date"] + " 仍未完成：\n" + "\n".join(_bullets(open_items, with_since=True))) if open_items else ""

    while blocks:
        text = "\n\n".join(blocks + ([tail] if tail else []))
        if len(text) <= char_budget or len(blocks) == 1:
            return text[:char_budget]
        blocks.pop(0)
    return tail[:char_budget]


# --- MemTrace sync ---------------------------------------------------------------


def _digest_turn_range(
    trace_store: TraceStore, project: str, digest_date: str, tz: tzinfo
) -> tuple[int, int] | None:
    """First and last turn number of that local day, from the local log."""
    day_turns = turns_by_local_date(
        trace_store.get_primary_session_turns(f"psess_{project}"), tz
    ).get(digest_date)
    if not day_turns:
        return None
    seqs = [int(t["turn_seq"]) for t in day_turns]
    return min(seqs), max(seqs)


def _link_digest(
    trace_store: TraceStore, memtrace_client, project: str, workspace_id: str, row: dict, tz: tzinfo
) -> None:
    """Make the digest navigable in MemTrace: `extends` the previous day's digest (a
    timeline to walk) and `extracted_from` every hourly draft holding turns of that day
    (from the summary back to its evidence). Raises on the first real failure; creating
    an edge MemTrace already has counts as done, so the whole step is safe to retry."""
    node_id = row["memtrace_node_id"]
    previous = trace_store.previous_synced_digest(project, workspace_id, row["digest_date"])
    if previous is not None:
        memtrace_client.create_edge(
            workspace_id=workspace_id,
            from_id=node_id,
            to_id=previous["memtrace_node_id"],
            relation="extends",
            stage="daily_digest",
        )
    day = _digest_turn_range(trace_store, project, row["digest_date"], tz)
    if day is None:
        return
    for draft in trace_store.list_memory_drafts(project, workspace_id):
        if draft["first_turn_seq"] <= day[1] and draft["last_turn_seq"] >= day[0]:
            memtrace_client.create_edge(
                workspace_id=workspace_id,
                from_id=node_id,
                to_id=draft["node_id"],
                relation="extracted_from",
                stage="daily_digest",
            )


def sync_digests_to_memtrace(
    trace_store: TraceStore, memtrace_client, project: str, workspace_id: str, tz: tzinfo | None = None
) -> int:
    """Write digests MemTrace doesn't have yet, refresh ones regenerated since, and move
    ones written to a workspace the project no longer uses; then link them (see
    _link_digest). Local SQLite stays the source of truth: a failed write or link is
    retried on the next pass and never blocks injection. Returns nodes written."""
    tz = tz or timezone.utc
    written = 0
    for row in trace_store.list_digests_needing_sync(project, workspace_id):
        title = digest_title(project, row["digest_date"])
        body = render_digest_markdown(project, row["digest_date"], row["digest"])
        if row["memtrace_node_id"] and row["memtrace_workspace_id"] == workspace_id:
            # Regenerated: update the node it already has rather than leaving the old
            # version beside a new one.
            memtrace_client.update_node(
                workspace_id=workspace_id,
                node_id=row["memtrace_node_id"],
                body=body,
                title=title,
                stage="daily_digest",
            )
            trace_store.mark_digest_synced(
                row["id"], workspace_id, row["memtrace_node_id"], new_node=False
            )
        else:
            node_id = memtrace_client.create_node(
                workspace_id=workspace_id,
                title=title,
                body=body,
                content_type="context",
                tags=["harness", "draft", "daily-digest"],
                force_create=True,
                stage="daily_digest",
            )
            trace_store.mark_digest_synced(row["id"], workspace_id, str(node_id), new_node=True)
        written += 1
    for row in trace_store.list_digests_needing_edges(project, workspace_id):
        _link_digest(trace_store, memtrace_client, project, workspace_id, row, tz)
        trace_store.mark_digest_edges_synced(row["id"])
    return written


# --- Preferences -----------------------------------------------------------------

_PREFERENCE_TRANSITIONS = {
    "adopt": (("pending",), "adopted"),
    "dismiss": (("pending",), "dismissed"),
    "retire": (("adopted",), "retired"),
    # Undo a retirement — the Harness retires rules on its own, so the operator needs a
    # way back that doesn't depend on the digest re-deriving the rule.
    "restore": (("retired",), "adopted"),
}


def resolve_preference(
    trace_store: TraceStore,
    rule_id: int,
    action: str,
    *,
    text: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """An operator action on a preference (the Harness also adopts and retires rules on
    its own; this is how the operator overrides that). Raises ValueError for an unknown
    action, a missing rule, or a rule not in a state that action applies to (e.g. already
    adopted by an earlier click)."""
    if action not in _PREFERENCE_TRANSITIONS:
        raise ValueError(f"unsupported action {action!r}")
    if scope is not None and scope not in ("global", "project"):
        raise ValueError(f"unsupported scope {scope!r}")
    expected, new_status = _PREFERENCE_TRANSITIONS[action]
    edited_text = text.strip() if text is not None else None
    if action == "adopt" and edited_text == "":
        raise ValueError("an adopted preference needs text")
    if not trace_store.update_preference_rule(
        rule_id,
        status=new_status,
        expected_statuses=expected,
        text=edited_text if action == "adopt" else None,
        scope=scope if action == "adopt" else None,
        retire_reason={"retire": "你在狀態網頁撤銷", "restore": ""}.get(action),
        adopted_by=ADOPTED_BY_OPERATOR if action in ("adopt", "restore") else None,
    ):
        rule = trace_store.get_preference_rule(rule_id)
        if rule is None:
            raise ValueError(f"preference {rule_id} not found")
        raise ValueError(f"preference {rule_id} is {rule['status']}, cannot {action}")
    rule = trace_store.get_preference_rule(rule_id)
    assert rule is not None
    return rule


def adopted_preferences_for(trace_store: TraceStore, project: str) -> list[dict]:
    return visible_rules(trace_store.list_preference_rules(statuses=("adopted",)), project)


def render_preference_rules(
    rules: list[dict], *, with_scope: bool = False, with_ids: bool = False
) -> str:
    by_category: dict[str, list[dict]] = {}
    for rule in rules:
        by_category.setdefault(rule["category"], []).append(rule)
    lines = []
    for category in PREFERENCE_CATEGORIES:
        if category not in by_category:
            continue
        lines.append(f"{PREFERENCE_CATEGORY_LABELS[category]}：")
        for rule in by_category[category]:
            scope = f"（僅 {rule['project']}）" if with_scope and rule["scope"] == "project" else ""
            label = f"[#{rule['id']}] " if with_ids else ""
            lines.append(f"- {label}{rule['text']}{scope}")
    return "\n".join(lines)


def preference_context_for(trace_store: TraceStore, project: str, *, with_ids: bool = False) -> str:
    """Newest MAX_PREFERENCES_IN_CONTEXT adopted rules, grouped by category."""
    rules = sorted(adopted_preferences_for(trace_store, project), key=lambda r: r["id"], reverse=True)
    return render_preference_rules(rules[:MAX_PREFERENCES_IN_CONTEXT], with_ids=with_ids)


PREFERENCE_CORRECT_MARKER = "HARNESS_PREFERENCE_CORRECT::"


def apply_chat_preference_correction(
    trace_store: TraceStore, project: str, payload: str, user_text: str, today: str
) -> str | None:
    """A correction the chat model judged the human just made. payload is
    "<id>::retire" or "<id>::replace::<corrected text>". Only an adopted rule this project
    can see is touched; anything malformed or stale is ignored (returns None) rather than
    guessed at. Returns a one-line confirmation to show the human."""
    parts = [p.strip() for p in payload.split("::", 2)]
    if len(parts) < 2 or not parts[0].lstrip("#").isdigit():
        return None
    rule_id, action = int(parts[0].lstrip("#")), parts[1].lower()
    rule = trace_store.get_preference_rule(rule_id)
    if rule is None or rule["status"] != "adopted" or rule not in visible_rules([rule], project):
        return None
    quote = user_text.strip().replace("\n", " ")[:120]
    if action == "retire":
        if not trace_store.update_preference_rule(
            rule_id, status="retired", expected_statuses=("adopted",),
            retire_reason=f"{today} 你在聊天中更正：{quote}",
        ):
            return None
        return f"🧠 已撤回偏好 #{rule_id}：{rule['text']}（到狀態網頁可以恢復）"
    if action == "replace" and len(parts) == 3 and parts[2]:
        new_id = trace_store.add_preference_candidate(
            project=rule["project"], scope=rule["scope"], category=rule["category"], text=parts[2],
            evidence=[{"date": today, "turn_seq": None, "quote": user_text.strip()[:300]}],
            explicit=True, source_digest_date=today, status="adopted", adopted_by=ADOPTED_BY_CHAT,
        )
        trace_store.update_preference_rule(
            rule_id, status="retired", expected_statuses=("adopted",),
            retire_reason=f"{today} 被 #{new_id} 取代（你在聊天中更正）",
        )
        return f"🧠 已更正偏好 #{rule_id}：{rule['text']} → {parts[2]}（新的是 #{new_id}）"
    return None


def sync_operator_profile(trace_store: TraceStore, memtrace_client, workspace_id: str) -> None:
    """Rewrite (never append to) the operator profile node from the adopted rules."""
    rules = trace_store.list_preference_rules(statuses=("adopted",))
    body = (
        "Operator preferences the Harness adopted from the operator's own conversations "
        "(rewritten whenever one is adopted, replaced or retired; the operator can retire "
        "any of them on the status page or by correcting it in chat).\n\n"
        + (render_preference_rules(rules, with_scope=True) or "（尚無已採用的偏好）")
    )
    existing = memtrace_client.search_nodes(
        workspace_id=workspace_id, query=OPERATOR_PROFILE_TITLE, stage="preference_sync"
    )
    node = next(
        (n for n in existing if isinstance(n, dict) and n.get("title") == OPERATOR_PROFILE_TITLE and n.get("id")),
        None,
    )
    if node:
        memtrace_client.update_node(
            workspace_id=workspace_id, node_id=str(node["id"]), body=body, stage="preference_sync"
        )
    else:
        memtrace_client.create_node(
            workspace_id=workspace_id,
            title=OPERATOR_PROFILE_TITLE,
            body=body,
            content_type="preference",
            tags=["harness", "operator-preference"],
            stage="preference_sync",
        )
