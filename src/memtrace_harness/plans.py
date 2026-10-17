"""The primary agent's step plans: validation, expectations and progress rendering.

HARNESS_PLAN carries a goal and ordered steps. Each step names a capability,
arguments and an expectation. PlanRunner executes read/change tools, declared
jobs, governed development, install proposals and scope proposals sequentially.
Every operation needing approval waits for its own single-use confirmation.
"""

from __future__ import annotations

import json
import re
from typing import Any

PLAN_MARKER = "HARNESS_PLAN::"
MAX_STEPS = 10
MAX_TITLE_CHARS = 120
MAX_ARGS_CHARS = 2000
MAX_EXPECT_CHARS = 300
EXPECT_KINDS = ("ok", "contains", "not_contains", "regex", "model")

# step statuses
PENDING, RUNNING, AWAITING, CHECKING, DONE, FAILED, SKIPPED = (
    "pending", "running", "awaiting", "checking", "done", "failed", "skipped",
)
# plan statuses
P_RUNNING, P_ATTENTION, P_DONE, P_CANCELLED = "running", "needs_attention", "done", "cancelled"


class PlanError(ValueError):
    pass


def parse_plan(payload: str, *, allowed_tool: Any) -> dict[str, Any]:
    """The normalized plan from a marker payload, or PlanError. ``allowed_tool(name)`` says whether a tool may be a step."""
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise PlanError("計畫不是有效的 JSON") from exc
    if not isinstance(data, dict):
        raise PlanError("計畫要是一個 JSON 物件")
    goal = str(data.get("goal") or "").strip()
    if not goal or len(goal) > 300:
        raise PlanError("計畫要有 goal（300 字以內）")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("計畫要有至少一個步驟")
    if len(raw_steps) > MAX_STEPS:
        raise PlanError(f"計畫最多 {MAX_STEPS} 步")
    steps = []
    for index, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, dict):
            raise PlanError(f"第 {index} 步要是物件")
        title = str(raw.get("title") or "").strip()
        tool = str(raw.get("tool") or "").strip()
        args = raw.get("args") or {}
        if not title or len(title) > MAX_TITLE_CHARS:
            raise PlanError(f"第 {index} 步要有 title（{MAX_TITLE_CHARS} 字以內）")
        reason = allowed_tool(tool)
        if reason:
            raise PlanError(f"第 {index} 步的工具 {tool!r} 不能放進計畫：{reason}")
        if not isinstance(args, dict) or len(json.dumps(args, ensure_ascii=False)) > MAX_ARGS_CHARS:
            raise PlanError(f"第 {index} 步的 args 要是不太大的物件")
        steps.append({"title": title, "tool": tool, "args": args, "expect": _normalize_expect(raw.get("expect"), index)})
    return {"goal": goal, "steps": steps}


def _normalize_expect(raw: Any, index: int) -> dict[str, str]:
    if raw in (None, ""):
        return {"kind": "ok", "value": ""}
    if isinstance(raw, str):
        raw = {"kind": "model", "value": raw}
    if not isinstance(raw, dict):
        raise PlanError(f"第 {index} 步的 expect 格式不對")
    kind, value = str(raw.get("kind") or "ok"), str(raw.get("value") or "")
    if kind not in EXPECT_KINDS:
        raise PlanError(f"第 {index} 步的 expect.kind 要是 {'、'.join(EXPECT_KINDS)} 其中之一")
    if kind != "ok" and not value:
        raise PlanError(f"第 {index} 步的 expect 需要 value")
    if len(value) > MAX_EXPECT_CHARS:
        raise PlanError(f"第 {index} 步的 expect.value 太長")
    if kind == "regex":
        try:
            re.compile(value, re.MULTILINE)
        except re.error as exc:
            raise PlanError(f"第 {index} 步的 regex 無效：{exc}") from exc
    return {"kind": kind, "value": value}


def check_expectation(expect: dict[str, str], result_text: str) -> tuple[bool | None, str]:
    """(achieved, reason). ``achieved`` is None when only a model can tell (kind ``model``). The caller has already
    made sure the step itself ran without error; ``ok`` therefore always passes."""
    kind, value = expect.get("kind", "ok"), expect.get("value", "")
    if kind == "ok":
        return True, "步驟執行成功"
    if kind == "contains":
        found = value.lower() in result_text.lower()
        return found, f"結果{'有' if found else '沒有'}包含「{value}」"
    if kind == "not_contains":
        found = value.lower() in result_text.lower()
        return (not found), f"結果{'有出現' if found else '沒有出現'}「{value}」"
    if kind == "regex":
        found = re.search(value, result_text, re.MULTILINE) is not None
        return found, f"結果{'符合' if found else '不符合'} /{value}/"
    return None, f"需要判斷：{value}"


_ICON = {PENDING: "⬜", RUNNING: "▶️", AWAITING: "🕓", CHECKING: "🔎", DONE: "✅", FAILED: "❌", SKIPPED: "⏭"}
_WAITING = {AWAITING: "（等你確認）", CHECKING: "（確認結果中）", RUNNING: "（執行中）"}


def render_progress(plan: dict, steps: list[dict]) -> tuple[str, list[list[dict[str, str]]]]:
    """The progress message and its buttons for the plan's current state."""
    done = sum(1 for s in steps if s["status"] in {DONE, SKIPPED})
    header = {
        P_RUNNING: "📋 計畫進行中",
        P_ATTENTION: "⚠️ 計畫停下來了，需要你決定",
        P_DONE: "📋 計畫完成",
        P_CANCELLED: "🛑 計畫已取消",
    }.get(plan["status"], "📋 計畫")
    lines = [f"{header}（{done}/{len(steps)}）", f"目標：{plan['goal']}", ""]
    for number, step in enumerate(steps, 1):
        lines.append(f"{_ICON.get(step['status'], '⬜')} {number}. {step['title']}{_WAITING.get(step['status'], '')}")
        if step["status"] == FAILED and step.get("note"):
            lines.append(f"    ↳ {step['note']}")
    keyboard: list[list[dict[str, str]]] = []
    pid = plan["plan_id"]
    if plan["status"] == P_ATTENTION:
        failed = next((s for s in steps if s["status"] == FAILED), None)
        if failed is not None:
            keyboard.append([
                {"label": "🔁 重試這一步", "id": f"plan_retry:{pid}"},
                {"label": "⏭ 略過這一步", "id": f"plan_skip:{pid}"},
            ])
    if plan["status"] in {P_RUNNING, P_ATTENTION}:
        keyboard.append([{"label": "🛑 取消計畫", "id": f"plan_cancel:{pid}"}])
    return "\n".join(lines), keyboard


def verdict(steps: list[dict]) -> str:
    if all(s["status"] == DONE for s in steps):
        return "達成"
    if any(s["status"] == DONE for s in steps) and not any(s["status"] == FAILED for s in steps):
        return "部分達成（有略過的步驟）"
    return "未達成"


def render_final(plan: dict, steps: list[dict]) -> str:
    lines = [f"📋 計畫結束：{verdict(steps)}", f"目標：{plan['goal']}", ""]
    for number, step in enumerate(steps, 1):
        evidence = (step.get("note") or "").strip().replace("\n", " ")
        lines.append(f"{_ICON.get(step['status'], '⬜')} {number}. {step['title']}" + (f" — {evidence[:160]}" if evidence else ""))
    return "\n".join(lines)


_SKIP_REGEX = re.compile(
    r"(?:skip\s+(?:that|this|the)?\s*step(?:\s+and\s+continue)?|"
    r"skip\s+and\s+continue|"
    r"\bskip\s+step\b|"
    r"\bskip\b|"
    r"(?:略過|跳過)(?:這一步|那一步|該步驟|此步驟|步驟)?(?:[並與及]?繼續)?|"
    r"(?:略過|跳過)|"
    r"^(?:請|麻煩)?(?:繼續|continue)[!！。.]*$|"
    r"(?:繼續執行|continue\s+plan)"
    r")",
    re.IGNORECASE,
)

_RETRY_REGEX = re.compile(
    r"(?:retry\s+(?:that|this|the)?\s*step|"
    r"\bretry\s+step\b|"
    r"\bretry\b|"
    r"重試(?:這一步|那一步|該步驟|此步驟|步驟)?|"
    r"重試"
    r")",
    re.IGNORECASE,
)

_CANCEL_REGEX = re.compile(
    r"(?:cancel\s+(?:the\s+)?plan|"
    r"取消計[畫劃]"
    r")",
    re.IGNORECASE,
)

_NEGATION_REGEX = re.compile(
    r"(?:不要|別|勿|不能|不可|無法|don't|do not|never|cannot|can't|couldn't|could not)\s*"
    r"(?:略過|跳過|skip|重試|retry|取消|cancel|繼續|continue)",
    re.IGNORECASE,
)


def match_plan_action(text: str) -> str | None:
    """Detect whether a natural-language message expresses plan_skip, plan_retry, or plan_cancel."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if _NEGATION_REGEX.search(cleaned):
        return None
    if _SKIP_REGEX.search(cleaned):
        return "plan_skip"
    if _RETRY_REGEX.search(cleaned):
        return "plan_retry"
    if _CANCEL_REGEX.search(cleaned):
        return "plan_cancel"
    return None

