from __future__ import annotations

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.schemas import ConflictRecord, HarnessSummary, ModelResponse, RunMode, TaskEnvelope
from memtrace_harness.trace_store import TraceStore


class HarnessRunner:
    def __init__(
        self,
        *,
        adapters: list[ModelAdapter],
        trace_store: TraceStore,
        memtrace_client: MemTraceClient | None = None,
    ) -> None:
        self.adapters = adapters
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client

    def run(self, task: TaskEnvelope, *, writeback: bool = False) -> HarnessSummary:
        trace_id = self.trace_store.create_run(task)
        responses = [adapter.run(task) for adapter in self.adapters]
        conflicts = detect_conflicts(responses)
        recommendation = build_recommendation(responses, conflicts)
        run_mode: RunMode = "draft_write" if writeback else "dry_run"
        writeback_node_id = None

        if writeback:
            if not self.memtrace_client:
                raise RuntimeError("writeback requested, but no MemTrace client is configured")
            writeback_node_id = self.memtrace_client.create_node(
                workspace_id=task.workspace_id,
                title=f"Harness draft: {task.goal[:72]}",
                body=render_memtrace_draft(task, responses, conflicts, recommendation, trace_id),
                content_type="inquiry",
            )

        summary = HarnessSummary(
            task=task,
            responses=responses,
            conflicts=conflicts,
            recommendation=recommendation,
            run_mode=run_mode,
            trace_id=trace_id,
            writeback_node_id=writeback_node_id,
        )
        self.trace_store.save_summary(summary)
        return summary


def detect_conflicts(responses: list[ModelResponse]) -> list[ConflictRecord]:
    if len(responses) < 2:
        return []

    human_decision_required = any(response.requires_human_decision for response in responses)
    explicit_disagreements = [
        disagreement
        for response in responses
        for disagreement in response.disagreements
        if disagreement.strip()
    ]
    if explicit_disagreements:
        return [
            ConflictRecord(
                kind="model_disagreement",
                description="At least one adapter reported explicit disagreement.",
                claim_refs=explicit_disagreements,
                needs_human_decision=True,
            )
        ]
    if human_decision_required:
        return [
            ConflictRecord(
                kind="human_gate",
                description="Adapters agree this result should remain draft-only until human review.",
                needs_human_decision=True,
            )
        ]
    return []


def build_recommendation(responses: list[ModelResponse], conflicts: list[ConflictRecord]) -> str:
    if conflicts:
        return "Write a draft summary and keep the issue open for human review."
    if all(not response.requires_human_decision for response in responses):
        return "Adapters converged without known blockers; still prefer draft writeback for v1."
    return "Keep as draft-first output."


def render_memtrace_draft(
    task: TaskEnvelope,
    responses: list[ModelResponse],
    conflicts: list[ConflictRecord],
    recommendation: str,
    trace_id: str,
) -> str:
    lines = [
        "## Harness Draft",
        "",
        f"- Trace: `{trace_id}`",
        f"- Source task: `{task.task_id}`",
        f"- Risk level: `{task.risk_level}`",
        "",
        "## Goal",
        "",
        task.goal,
        "",
        "## Context Refs",
        "",
    ]
    lines.extend([f"- `{ref}`" for ref in task.context_refs] or ["- none"])
    lines.extend(["", "## Claims", ""])
    for response in responses:
        lines.append(f"### {response.adapter_id} ({response.role})")
        for claim in response.claims:
            refs = ", ".join(f"`{ref}`" for ref in claim.evidence_refs) or "none"
            lines.extend(
                [
                    f"- Claim: {claim.claim}",
                    f"  Evidence: {refs}",
                    f"  Confidence: {claim.confidence:.2f}; risk: {claim.risk}",
                ]
            )
        lines.append("")

    lines.extend(["## Conflicts / Human Gate", ""])
    if conflicts:
        for conflict in conflicts:
            lines.append(f"- {conflict.kind}: {conflict.description}")
    else:
        lines.append("- No explicit conflict detected by the v1 harness.")

    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            recommendation,
            "",
            "## Status",
            "",
            "Draft-only harness output. Do not treat as formal MemTrace knowledge until human review.",
        ]
    )
    return "\n".join(lines)
