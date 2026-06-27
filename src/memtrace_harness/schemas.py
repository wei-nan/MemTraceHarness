from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


RiskLevel = Literal["low", "medium", "high"]
RunMode = Literal["dry_run", "draft_write"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    workspace_id: str
    goal: str
    context_refs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)
    risk_level: RiskLevel = "medium"
    source: str = "manual"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelClaim:
    claim: str
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float = 0.5
    risk: RiskLevel = "medium"
    missing_evidence: str | None = None
    proposed_next_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelResponse:
    adapter_id: str
    role: str
    claims: list[ModelClaim]
    disagreements: list[str] = field(default_factory=list)
    requires_human_decision: bool = True
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["claims"] = [claim.to_dict() for claim in self.claims]
        return data


@dataclass(frozen=True)
class ConflictRecord:
    kind: str
    description: str
    claim_refs: list[str] = field(default_factory=list)
    needs_human_decision: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HarnessSummary:
    task: TaskEnvelope
    responses: list[ModelResponse]
    conflicts: list[ConflictRecord]
    recommendation: str
    run_mode: RunMode
    trace_id: str
    writeback_node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "responses": [response.to_dict() for response in self.responses],
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "recommendation": self.recommendation,
            "run_mode": self.run_mode,
            "trace_id": self.trace_id,
            "writeback_node_id": self.writeback_node_id,
        }
