"""Short operator-facing updates. Detailed artifacts remain in the trace store."""
from __future__ import annotations

import re
from typing import Any


def brief(value: Any, limit: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = re.sub(r"\s+", " ", text.replace("`", "")).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    end = max(clipped.rfind(mark) for mark in ("。", "！", "；"))
    return clipped[:end + 1] if end >= limit // 2 else clipped.rstrip("，、； ") + "…"


def stage_parts(stage: str) -> tuple[int, str]:
    match = re.match(r"^p(\d+)-(.+)$", stage)
    phase, name = (int(match[1]), match[2]) if match else (1, stage)
    return phase, re.sub(r"^(progress|converge)-\d+$", r"\1", name)


def verification_text(artifact: dict[str, Any]) -> str:
    rows = artifact.get("repo_verifications")
    statuses = [row.get("status") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    if not statuses:
        statuses = [artifact.get("verification_status")]
    if "regression" in statuses or artifact.get("verdict") == "REJECT":
        return "檢查還沒通過，已記下需要處理的問題。"
    if "could_not_run" in statuses:
        return "有些檢查未能執行，結果仍待確認。"
    if "baseline_failing" in statuses:
        return "檢查發現原本就存在的問題，這次沒有新增同類失敗。"
    if statuses and all(status == "passed" for status in statuses):
        return "這次的自動檢查已通過。"
    return "已取得檢查結果，接下來會核對是否符合需求。"


def delivery_text(delivery: Any) -> str:
    if not isinstance(delivery, dict):
        return ""
    state = delivery.get("state")
    if delivery.get("remote"):
        rows = delivery.get("repos") or []
        if state == "merged":
            return "已確認 PR 合併進遠端 main，相關 issue 已關閉，worktree 已清理。"
        if state == "partial":
            merged = "、".join(row["name"] for row in rows if row.get("merged"))
            pending = "、".join(row["name"] for row in rows if not row.get("merged"))
            return f"遠端已合併：{merged or '無'}；仍待交付／收尾：{pending or '關單或清理'}。"
        if state == "paused":
            return "自動交付已暫停，PR、分支與進度已保留。"
        if state == "blocked":
            return "交付尚未完成，PR 或獨立分支已保留。"
    if state == "merged":
        if len(delivery.get("repos") or []) > 1:
            return "變更已整合回各專案原本的本機分支。"
        branch = delivery.get("target_branch")
        target = "本機主分支" if branch in {"main", "master"} else (f"本機 {branch} 分支" if branch else "原本的本機分支")
        return f"變更已整合回{target}。"
    if state == "partial":
        merged = "、".join(delivery.get("merged_repos") or [])
        pending = "、".join(delivery.get("pending_repos") or [])
        return f"已整合：{merged}；仍待整合：{pending}。"
    if state in {"unmerged", "blocked"}:
        return "變更保留在獨立分支，尚待整合。"
    return ""


class StageNarrator:
    def __init__(self) -> None:
        self.phases: list[dict[str, Any]] = []
        self.active_phases: dict[int, dict[str, Any]] = {}

    def render(self, stage: str, state: str, artifact: dict[str, Any] | None) -> str:
        data = artifact if isinstance(artifact, dict) else {}
        phase, name = stage_parts(stage)
        planned = data.get("goal_plan")
        if isinstance(planned, dict) and isinstance(planned.get("phases"), list):
            self.phases = [item for item in planned["phases"] if isinstance(item, dict)]
        current = data.get("phase")
        if isinstance(current, dict):
            self.active_phases[phase] = current
        if not isinstance(current, dict):
            current = self.active_phases.get(phase)
        if not isinstance(current, dict):
            current = self.phases[phase - 1] if phase <= len(self.phases) else {}
        purpose = brief(current.get("purpose") or current.get("title"), 90)
        reported = brief(data.get("user_summary"))

        if state == "running":
            if name == "control-start":
                return "我會先整理你的需求，確認要做的事與完成條件。"
            if name.startswith("plan"):
                return "我會先確認可行做法，安排接下來的處理步驟。"
            if name.startswith("develop"):
                lead = "我會依檢查發現的問題再做修正" if "revision" in name else "接下來會依計畫處理"
                return f"{lead}：{purpose}" if purpose else lead + "，完成後再檢查結果。"
            if name.startswith("verify"):
                return "我會重新執行檢查，確認這次修改的結果。"
            if name == "final-gate":
                return "我會再確認修改範圍，檢查是否碰到受保護的內容。"
            if name.startswith("g1"):
                return "我會檢查處理計畫是否符合需求，有沒有遺漏或多做。"
            if name.startswith("g2"):
                return "我會另外檢查實作與測試證據，確認是否符合原先需求。"
            if name == "progress":
                return "我會核對目前成果，確認下一步是否需要調整。"
            if name == "converge":
                return "我會核對整體需求與檢查結果，整理這次完成與待處理的事。"
            if name == "integrate":
                return "我會把通過檢查的變更整合回本機主分支。"
            if name == "operate":
                return "我會執行這次交辦的操作，並確認執行結果。"
            return "我會接著處理這一步，完成後回報結果。"

        if name == "integrate":
            return delivery_text(data.get("delivery")) or "變更尚未完成整合，現有工作已保留。"
        if state not in {"succeeded", "reused"}:
            return "這一步還沒有取得可用的結果，尚未完成。"
        if name.startswith("verify"):
            return verification_text(data)
        if data.get("status") in {"failed", "needs_human"} or data.get("verdict") in {"REJECT", "NEEDS_HUMAN"}:
            return "這一步還有問題需要處理。" + (reported or brief(data.get("summary"), 120))
        if data.get("action") in {"ask_human", "stop"}:
            return "已整理出需要你決定的事項，接下來會說明。"
        if name == "control-start":
            if reported:
                return "已確認處理方向：" + reported
            titles = " → ".join(brief(item.get("title"), 55) for item in self.phases[:3])
            if titles:
                return "已排好步驟：" + brief(titles, 160)
            return "已確認這次的處理方向。"
        if name.startswith("develop") or name == "operate":
            detail = reported or brief(data.get("summary"))
            return ("這一步的結果：" + detail) if detail else "已完成這一步的處理，接著檢查結果。"
        if name == "final-gate":
            if data.get("status") == "skipped":
                return "這次沒有需要檢查的檔案變更。"
            if data.get("findings"):
                return "已檢查修改範圍與受保護內容，另有提醒事項留在紀錄中。"
            return "已檢查修改範圍與受保護內容，沒有發現需要擋下的問題。"
        if name == "converge":
            # The goal check and merge happen after this model turn, so don't announce completion yet.
            return "已整理完成項目與尚待確認的事項，接著確認最終結果。"
        if name.startswith(("g1", "g2")):
            return "已檢查計畫與需求。" + reported if name.startswith("g1") else "已檢查實作與測試證據。" + reported
        if name.startswith("plan"):
            return "已整理好可執行的處理步驟。" + reported
        if name == "progress":
            return "已核對目前成果，並調整後續步驟。" if data.get("action") == "revise_phases" else "已核對目前成果，接下來依計畫繼續。"
        return reported or "這一步已有結果，接下來會繼續確認。"


def outcome_message(project: str, summary: Any) -> str:
    """Compact chat view; never mutate or replace the full durable recommendation."""
    status = summary.status
    heading = {
        "succeeded": f"✅「{project}」這次工作已完成。",
        "delivering": f"⏳「{project}」開發已通過檢查，交付尚未完成。",
        "failed": f"⚠️「{project}」這次工作未完成。",
        "needs_human": f"⚠️「{project}」還有事情需要你確認。",
        "budget_exhausted": f"⚠️「{project}」這次執行已暫停。",
    }.get(status, f"⚠️「{project}」這次執行已停止。")
    stages = summary.stages if isinstance(summary.stages, list) else []
    meaningful = [item for item in stages if isinstance(getattr(item, "artifact", None), dict)]
    development = [item for item in meaningful if stage_parts(item.stage)[1].startswith("develop")]
    if not development:
        # Operational responses can contain essential holdings/counts; keep their short layout.
        rec = summary.recommendation if isinstance(summary.recommendation, str) else ""
        rec = rec.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
        return heading + ("\n" + rec[:500] + ("…" if len(rec) > 500 else "") if rec else "")
    parts = [heading]
    if status != "succeeded":
        parts.append(brief(summary.recommendation, 240))
    else:
        closing = next((item.artifact for item in reversed(meaningful) if stage_parts(item.stage)[1] == "converge"), {})
        detail = brief(closing.get("user_summary"))
        if not detail:
            detail = brief(development[-1].artifact.get("user_summary") or development[-1].artifact.get("summary"))
        if detail:
            parts.append(detail)
    latest_checks: dict[int, dict[str, Any]] = {}
    for item in meaningful:
        if item.artifact.get("verification_status"):
            latest_checks[stage_parts(item.stage)[0]] = item.artifact
    if latest_checks:
        for text in dict.fromkeys(verification_text(item) for item in latest_checks.values()):
            parts.append(text)
    delivered = delivery_text(getattr(summary, "delivery", None))
    if delivered:
        parts.append(delivered)
    # Never bury outstanding work behind a successful-stage headline.
    gaps = [gap for item in development[-1:] for gap in item.artifact.get("gaps", []) if isinstance(gap, str)]
    if gaps:
        parts.append("仍待確認：" + brief("；".join(gaps), 120))
    return "\n".join(part for part in parts if part)
