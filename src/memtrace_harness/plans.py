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
EXPECT_KINDS = ("ok", "contains", "not_contains", "regex", "model", "commit_alignment")

# step statuses
PENDING, RUNNING, AWAITING, CHECKING, DONE, FAILED, SKIPPED = (
    "pending", "running", "awaiting", "checking", "done", "failed", "skipped",
)
# plan statuses
P_RUNNING, P_ATTENTION, P_DONE, P_CANCELLED = "running", "needs_attention", "done", "cancelled"

# stage categories
STAGE_SETUP = "工作設定"
STAGE_EXEC = "執行"
STAGE_READBACK = "讀取結果"


def step_stage(step: dict) -> str:
    """Classify a step into setup (計畫/工作設定), execution (執行), or readback (讀取結果)."""
    s = str(step.get("stage") or "").strip().lower()
    if s in {"setup", "job_setup", "工作設定", "計畫/工作設定", "計畫設定"}:
        return STAGE_SETUP
    if s in {"exec", "execution", "執行", "執行中"}:
        return STAGE_EXEC
    if s in {"readback", "read", "讀取", "讀取結果", "檢查", "確認"}:
        return STAGE_READBACK
    tool = str(step.get("tool") or "").strip()
    if tool in {"propose_scope_change", "request_install"}:
        return STAGE_SETUP
    if tool in {"read_file", "ops_read", "browse", "read_log", "query_db", "system_status", "http_probe", "list_jobs"}:
        return STAGE_READBACK
    return STAGE_EXEC


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
    raw_criteria = data.get("success_criteria") or data.get("criteria")
    criteria: list[str] = []
    if isinstance(raw_criteria, list):
        criteria = [str(c).strip() for c in raw_criteria if str(c).strip()]
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
        step_dict = {"title": title, "tool": tool, "args": args, "expect": _normalize_expect(raw.get("expect"), index)}
        stage = str(raw.get("stage") or "").strip()
        if stage:
            step_dict["stage"] = stage
        steps.append(step_dict)
    return {"goal": goal, "criteria": criteria, "steps": steps}


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
    if kind == "commit_alignment":
        lower_text = result_text.lower()
        has_pipeline = any(kw in lower_text for kw in ("pipeline", "passed", "success", "running", "failed", "ci", "#"))
        val_lower = value.lower()
        has_commit = val_lower in lower_text
        if has_commit and has_pipeline:
            return True, f"管線狀態已確認且 commit 符合「{value}」"
        if not has_commit:
            return False, f"輸出未包含預期 commit「{value}」"
        return False, "輸出未包含可識別的管線狀態"
    return None, f"需要判斷：{value}"


_ICON = {PENDING: "⬜", RUNNING: "▶️", AWAITING: "🕓", CHECKING: "🔎", DONE: "✅", FAILED: "❌", SKIPPED: "⏭"}


def _step_label_suffix(step: dict) -> str:
    status = step.get("status")
    stage = step_stage(step)
    if status == AWAITING:
        if stage == STAGE_SETUP:
            return "（工作設定：等你確認）"
        if stage == STAGE_READBACK:
            return "（讀取結果：等你確認）"
        return "（等你確認）"
    if status == RUNNING:
        if stage == STAGE_SETUP:
            return "（工作設定中）"
        if stage == STAGE_READBACK:
            return "（讀取結果中）"
        return "（執行中）"
    if status == CHECKING:
        return "（讀取結果中）"
    return ""


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
    if plan.get("criteria"):
        lines.append("驗收條件：" + "；".join(plan["criteria"]))
        lines.append("")
    for number, step in enumerate(steps, 1):
        suffix = _step_label_suffix(step)
        lines.append(f"{_ICON.get(step['status'], '⬜')} {number}. {step['title']}{suffix}")
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


def get_unresolved_criteria(plan: dict, steps: list[dict]) -> list[str]:
    """Identify which success criteria remain unresolved based on completed steps and output."""
    criteria = plan.get("criteria") or []
    if not criteria:
        return [f"{s['title']}（未完成）" for s in steps if s.get("status") != DONE]

    unresolved = []
    done_steps = [s for s in steps if s.get("status") == DONE]
    for crit in criteria:
        crit_lower = crit.lower()
        matched = False
        for s in done_steps:
            title_lower = (s.get("title") or "").lower()
            note_lower = (s.get("note") or "").lower()
            res_lower = (s.get("result_text") or "").lower()
            commits = re.findall(r"\b[0-9a-f]{7,40}\b", crit_lower)
            if commits:
                if all(c in res_lower or c in note_lower or c in title_lower for c in commits):
                    repos = re.findall(r"\bcdmp(?:_[a-z0-9_]+)?\b", crit_lower)
                    if not repos or any(r in res_lower or r in note_lower or r in title_lower for r in repos):
                        matched = True
                        break
            else:
                if crit_lower in title_lower or crit_lower in note_lower or title_lower in crit_lower:
                    matched = True
                    break
        if not matched:
            unresolved.append(crit)
    return unresolved


def get_last_concrete_error(steps: list[dict]) -> str | None:
    """Return the last concrete failure note from the plan steps."""
    for s in reversed(steps):
        note = s.get("note") or ""
        if s.get("status") == FAILED and note:
            return note
        if note and any(err_kw in note.lower() for err_kw in ("error", "failed", "失敗", "command not found", "not found", "unknown flag", "未包含", "cannot parse", "127")):
            return note
        res = s.get("result_text") or ""
        if res:
            first_line = res.strip().split("\n")[0]
            if any(err_kw in first_line.lower() for err_kw in ("error", "failed", "失敗", "command not found", "not found", "unknown flag", "cannot parse")):
                return first_line
    return None


def verdict(steps: list[dict], criteria: list[str] | None = None) -> str:
    all_done = all(s["status"] == DONE for s in steps)
    if all_done:
        if criteria:
            dummy_plan = {"criteria": criteria}
            unresolved = get_unresolved_criteria(dummy_plan, steps)
            if unresolved:
                return "部分達成（有未滿足的條件）"
        return "達成"
    if any(s["status"] == DONE for s in steps) and not any(s["status"] == FAILED for s in steps):
        return "部分達成（有略過的步驟）"
    return "未達成"


def render_final(plan: dict, steps: list[dict]) -> str:
    v = verdict(steps, plan.get("criteria"))
    lines = [f"📋 計畫結束：{v}", f"目標：{plan['goal']}", ""]
    for number, step in enumerate(steps, 1):
        evidence = (step.get("note") or "").strip().replace("\n", " ")
        lines.append(f"{_ICON.get(step['status'], '⬜')} {number}. {step['title']}" + (f" — {evidence[:160]}" if evidence else ""))

    unresolved = get_unresolved_criteria(plan, steps)
    if unresolved and v != "達成":
        lines.append("")
        lines.append("未滿足條件：" + "；".join(unresolved))

    last_err = get_last_concrete_error(steps)
    if last_err and v != "達成":
        if not unresolved:
            lines.append("")
        lines.append(f"最後具體錯誤：{last_err}")

    return "\n".join(lines)
