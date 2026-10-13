"""The Controller's plan for the user's whole goal, and the check that the goal was actually reached.

A request is rarely one unit of work, and a loop that answers only the first part of it must not report the
whole as done. So the Controller does two things the Harness holds it to:

1. At the start it writes a *goal plan*: what the user's original request means, the observable
   ``success_criteria`` that would make it done, and the ordered ``phases`` that get there. Each phase names the
   skills it needs (``plan``, ``develop``, or both) and the acceptance conditions of that phase. A phase may skip
   ``plan`` (the Controller assigns the work directly) or be a ``plan``-only phase (design first, build later).
   ``review`` is never chosen: it follows every plan (G1) and every development (G2) automatically.
2. After each phase it reports a *goal check* against the success criteria. The Harness refuses to finish the
   task while the check does not cover every criterion, says anything is unmet, or is missing altogether, and
   turns that into more phases (bounded) or a question for the operator instead of a "done".

This module is the pure part: parsing and validating what the Controller returned, judging a goal check, and
rendering it for the person who made the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_PHASES = 6
MAX_CRITERIA = 8
MAX_TEXT = 600
PHASE_SKILL_SETS = (("plan", "develop"), ("plan",), ("develop",))
CHECK_VERDICTS = ("met", "partly_met", "not_met")
CRITERION_STATES = ("met", "unmet", "unverified")


@dataclass(frozen=True)
class Phase:
    title: str
    purpose: str
    skills: tuple[str, ...]
    acceptance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "purpose": self.purpose,
            "skills": list(self.skills),
            "acceptance": list(self.acceptance),
        }


@dataclass(frozen=True)
class GoalPlan:
    restated_goal: str
    success_criteria: tuple[str, ...]
    phases: tuple[Phase, ...]
    # True when the Controller gave no usable plan and the Harness made the one-phase default.
    defaulted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "restated_goal": self.restated_goal,
            "success_criteria": list(self.success_criteria),
            "phases": [phase.to_dict() for phase in self.phases],
        }


@dataclass(frozen=True)
class GoalVerdict:
    complete: bool
    unmet: tuple[str, ...] = ()
    unverified: tuple[str, ...] = ()
    problem: str = ""


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit else None


def _text_list(value: Any, max_items: int, limit: int = MAX_TEXT) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > max_items:
        return None
    items = [_text(item, limit) for item in value]
    return None if any(item is None for item in items) else tuple(item for item in items if item)


def parse_phases(value: Any, *, limit: int = MAX_PHASES) -> tuple[Phase, ...] | None:
    """The phases in a Controller answer, or None when any of them is malformed (a half-understood plan is
    worse than the default one)."""
    if not isinstance(value, list) or not value or len(value) > limit:
        return None
    phases = []
    for raw in value:
        if not isinstance(raw, dict):
            return None
        title, purpose = _text(raw.get("title"), 120), _text(raw.get("purpose"))
        skills = raw.get("skills")
        acceptance = _text_list(raw.get("acceptance", []), 6)
        if not title or not purpose or acceptance is None:
            return None
        if not isinstance(skills, list) or tuple(skills) not in PHASE_SKILL_SETS:
            return None
        phases.append(Phase(title=title, purpose=purpose, skills=tuple(skills), acceptance=acceptance))
    return tuple(phases)


def parse_goal_plan(value: Any) -> GoalPlan | None:
    if not isinstance(value, dict):
        return None
    restated = _text(value.get("restated_goal"))
    criteria = _text_list(value.get("success_criteria"), MAX_CRITERIA)
    phases = parse_phases(value.get("phases"))
    if not restated or not criteria or phases is None:
        return None
    return GoalPlan(restated_goal=restated, success_criteria=criteria, phases=phases)


def default_goal_plan(goal: str) -> GoalPlan:
    """One plan-then-build phase for the whole goal: what a run did before the Controller planned phases."""
    text = " ".join(goal.split())[:MAX_TEXT] or "the requested change"
    return GoalPlan(
        restated_goal=text,
        success_criteria=(),
        phases=(Phase(title="Do the work", purpose=text, skills=("plan", "develop")),),
        defaulted=True,
    )


def assignment_plan(phase: Phase) -> dict[str, Any]:
    """The plan artifact for a phase the Controller assigned straight to ``develop`` (no ``plan`` skill ran):
    same shape as a plan, so the rest of the pipeline treats it like one."""
    return {
        "status": "ready",
        "plan": phase.purpose,
        "acceptance_criteria": list(phase.acceptance) or [phase.purpose],
        "open_questions": [],
        "scope_exclusions": [],
        "assigned_by": "controller",
    }


def judge_goal_check(plan: GoalPlan | None, check: Any) -> GoalVerdict:
    """Whether a goal check lets the task finish. With no success criteria on record (an older run, or a
    default plan) there is nothing to hold the check to, so a missing check passes; once the Controller wrote
    criteria, the check must answer every one of them."""
    required = len(plan.success_criteria) if plan else 0
    if not isinstance(check, dict):
        if required:
            return GoalVerdict(False, problem="Controller 沒有逐項回報目標的完成度")
        return GoalVerdict(True)
    verdict = check.get("verdict")
    entries = check.get("criteria")
    if verdict not in CHECK_VERDICTS or not isinstance(entries, list):
        return GoalVerdict(False, problem="Controller 回報的目標完成度格式不對")
    unmet: list[str] = []
    unverified: list[str] = []
    answered = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") not in CRITERION_STATES:
            return GoalVerdict(False, problem="Controller 回報的目標完成度格式不對")
        answered += 1
        name = str(entry.get("criterion") or "").strip() or "(未命名的條件)"
        if entry["status"] == "unmet":
            unmet.append(name)
        elif entry["status"] == "unverified":
            unverified.append(name)
    remaining = check.get("remaining")
    if isinstance(remaining, list):
        unmet.extend(str(item).strip() for item in remaining if str(item).strip())
    if answered < required:
        return GoalVerdict(
            False,
            tuple(unmet),
            tuple(unverified),
            f"目標有 {required} 個成功條件，Controller 只回報了 {answered} 個",
        )
    if verdict != "met" or unmet:
        return GoalVerdict(False, tuple(unmet), tuple(unverified), "目標還沒有完全達成")
    return GoalVerdict(True, (), tuple(unverified))


def render_goal_report(plan: GoalPlan | None, check: Any, verdict: GoalVerdict | None = None) -> str:
    """The part of the final message that says whether the user's original goal was reached, criterion by
    criterion. Empty when there is nothing to report."""
    if not isinstance(check, dict) or not isinstance(check.get("criteria"), list):
        return ""
    icons = {"met": "✅", "unmet": "❌", "unverified": "❔"}
    lines = []
    if plan and plan.restated_goal:
        lines.append(f"目標：{plan.restated_goal}")
    for entry in check["criteria"]:
        if not isinstance(entry, dict):
            continue
        evidence = str(entry.get("evidence") or "").strip()
        line = f"{icons.get(entry.get('status'), '•')} {str(entry.get('criterion') or '').strip()}"
        lines.append(f"{line}（{evidence}）" if evidence else line)
    remaining = check.get("remaining")
    if isinstance(remaining, list) and remaining:
        lines.append("還沒做完：" + "；".join(str(item).strip() for item in remaining if str(item).strip()))
    if verdict is not None and verdict.unverified:
        lines.append("以下條件沒有被獨立驗證，請你自己確認：" + "；".join(verdict.unverified))
    return "\n".join(lines)
