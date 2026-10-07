"""What the operator decided, kept as precedent.

Every time the operator answers a request the harness put to them (a decision card, a scanner
proposal, a question), one row is written: what was asked, what they chose, and whether that
followed the recommendation. Two consumers read it:

* the Controller, which gets a short "precedent" block next to the operator's adopted
  preferences, so a judgment about routing or notifying starts from how this person has
  actually decided before; and
* the nightly digest, which sees each button-tap decision as an ordinary user turn in the
  conversation log (see decision_turn_text()) and can derive a preference from it, citing
  that turn, through the evidence rules it already has.

A decision where the operator chose differently from the recommendation is the strongest
signal, so those are always listed ahead of the ones that simply agreed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from memtrace_harness.decision_card import option_letter

if TYPE_CHECKING:
    from memtrace_harness.approval import ApprovalRequestData
    from memtrace_harness.trace_store import TraceStore

# Requests that carry no judgment of the operator's: a retry/abandon on a parse glitch, a
# settings block, a spent budget. Recording them would only teach the Controller noise.
UNRECORDED_REASONS = frozenset({"model_output_invalid", "config_change_required", "budget_exhausted"})

# The content_type of the context item carrying the block below. Only the Controller is
# given it (loop.py); the working roles never see the operator's decisions or preferences.
PRECEDENT_CONTENT_TYPE = "operator_precedent"

MAX_PRECEDENT_LINES = 8
MAX_DEVIATIONS_FIRST = 4
_SITUATION_CHARS = 160
_CHOICE_CHARS = 160
_REASON_CHARS = 160

_ID_PATTERN = re.compile(r"\b[DP]\d+\b")


def record_resolution(
    trace_store: TraceStore,
    req: ApprovalRequestData,
    *,
    project: str,
    action: str,
    answer: str | None = None,
    chosen_index: int | None = None,
) -> tuple[int, str] | None:
    """Write the decision row for a resolved request. Returns (record id, text for the
    conversation log) when the decision was a button tap — a free-text answer is already in
    the log as the operator's own message — and None otherwise. Never raises: a record is
    best-effort, the resolution it describes has already happened."""
    if req.reason in UNRECORDED_REASONS:
        return None
    card = req.decision_card
    answer_text = (answer or "").strip() or None
    options = card["options"] if card else None
    recommended = card["recommended"] if card else None
    action = action.lower()
    chosen_text = None
    if action == "reject":
        outcome = "abandoned" if card else "rejected"
        followed = None if card else False
    elif action == "approve":
        outcome, followed = "approved", True
    elif chosen_index is not None and card and 0 <= chosen_index < len(card["options"]):
        option = card["options"][chosen_index]
        chosen_text = f"{option_letter(chosen_index)}「{option['label']}」"
        followed = chosen_index == recommended
        outcome = "picked_recommended" if followed else "picked_other"
    elif card:
        outcome, followed, chosen_text = "free_text", False, answer_text
    else:
        outcome, followed, chosen_text = "answered", None, answer_text
    situation = card["situation"] if card else req.proposed_action
    record_id = trace_store.add_decision_record(
        project=project,
        kind=req.reason,
        situation=situation.strip(),
        outcome=outcome,
        approval_id=req.id,
        conversation_id=req.conversation_id,
        options=options,
        recommended=recommended,
        chosen_index=chosen_index,
        chosen_text=chosen_text,
        followed=followed,
        reason=answer_text if outcome == "free_text" else None,
    )
    if outcome in {"free_text", "answered"}:
        return None
    return record_id, decision_turn_text(
        record_id=record_id,
        kind=req.reason,
        situation=situation,
        outcome=outcome,
        chosen_text=chosen_text,
        recommended_label=_recommended_label(options, recommended),
    )


def _recommended_label(options: list[dict] | None, recommended: int | None) -> str | None:
    if not options or recommended is None:
        return None
    return f"{option_letter(recommended)}「{options[recommended]['label']}」"


_OUTCOME_TEXT = {
    "approved": "核准",
    "rejected": "拒絕",
    "abandoned": "放棄這個任務",
}


def decision_turn_text(
    *,
    record_id: int,
    kind: str,
    situation: str,
    outcome: str,
    chosen_text: str | None,
    recommended_label: str | None,
) -> str:
    choice = chosen_text or _OUTCOME_TEXT.get(outcome, outcome)
    text = f"【決策 D{record_id}】{kind}：{_clip(situation, 200)} → 我選：{choice}"
    if outcome == "picked_other" and recommended_label:
        text += f"（建議是 {recommended_label}，我沒有照建議）"
    return text


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _line(record: dict[str, Any]) -> str:
    day = record["created_at"][:10]
    outcome = record["outcome"]
    choice = record["chosen_text"] or _OUTCOME_TEXT.get(outcome, outcome)
    line = f"- D{record['id']} · {day} · {record['kind']} · {_clip(record['situation'], _SITUATION_CHARS)}"
    line += f" → {_clip(choice, _CHOICE_CHARS)}"
    if record["followed"] is False and outcome != "rejected":
        options, rec = record["options"], record["recommended"]
        label = _recommended_label(options, rec)
        line += f" ⚠ 不同於建議{f'（{label}）' if label else ''}"
    elif record["followed"] is False:
        line += " ⚠ 拒絕了提案"
    if record["reason"]:
        line += f" · 原因：{_clip(record['reason'], _REASON_CHARS)}"
    return line


def select_precedents(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records are newest first. Disagreements go in first (up to MAX_DEVIATIONS_FIRST), then
    the most recent of the rest fill the remaining lines; the result is shown newest first."""
    deviations = [r for r in records if r["followed"] is False][:MAX_DEVIATIONS_FIRST]
    chosen = {r["id"] for r in deviations}
    rest = [r for r in records if r["id"] not in chosen][: MAX_PRECEDENT_LINES - len(deviations)]
    return sorted([*deviations, *rest], key=lambda r: r["id"], reverse=True)


def precedent_block(
    trace_store: TraceStore, project: str, *, preferences: str = "", kind: str | None = None
) -> str:
    """The text of the Controller's precedent item, or "" when there is nothing to say."""
    records = select_precedents(trace_store.list_decision_records(project, kind=kind, limit=40))
    parts: list[str] = []
    if records:
        parts.append(
            "This operator's recent decisions in this project, newest first (D<n>). ⚠ marks a "
            "decision that went against a recommendation or proposal like yours:\n"
            + "\n".join(_line(r) for r in records)
        )
    if preferences.strip():
        parts.append(
            "Standing preferences the harness derived from this operator and adopted ([#n] is the "
            "preference's number, cite it as P<n>; any may be wrong, and an "
            "explicit instruction in the current request outranks all of them):\n"
            + preferences.strip()
        )
    return "\n\n".join(parts)


def known_basis_ids(text: str) -> set[str]:
    """The D<n>/P<n> ids a precedent block actually offered, so a card cannot cite one that
    was never in front of the model."""
    ids = {m for m in _ID_PATTERN.findall(text)}
    # Preferences are listed as "[#3]"; accept them as P3.
    ids.update(f"P{m}" for m in re.findall(r"\[#(\d+)\]", text))
    return ids
