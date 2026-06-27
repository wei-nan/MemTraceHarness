from __future__ import annotations

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.schemas import ModelClaim, ModelResponse, TaskEnvelope


class MockModelAdapter(ModelAdapter):
    def __init__(self, adapter_id: str, role: str) -> None:
        self.adapter_id = adapter_id
        self.role = role

    def run(self, task: TaskEnvelope) -> ModelResponse:
        evidence_refs = task.context_refs[:3]
        hydrated_count = len([item for item in task.context_items if item.body])
        context_note = (
            f" Hydrated context items available: {hydrated_count}."
            if task.context_items
            else " Context refs were not hydrated."
        )
        claim = ModelClaim(
            claim=(
                f"{self.role} view: handle '{task.goal}' as a draft-first MemTrace "
                f"workflow with explicit evidence and human review.{context_note}"
            ),
            evidence_refs=evidence_refs,
            confidence=0.72,
            risk=task.risk_level,
            proposed_next_action="Create a draft summary and keep formal resolution human-gated.",
        )
        return ModelResponse(
            adapter_id=self.adapter_id,
            role=self.role,
            claims=[claim],
            disagreements=[],
            requires_human_decision=True,
            raw_text=claim.claim,
        )
