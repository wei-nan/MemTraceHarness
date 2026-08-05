from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


RiskLevel = Literal["low", "medium", "high"]
RunMode = Literal["cli_run", "cli_run_draft_write", "agent_loop", "agent_loop_draft_write"]
ProviderId = Literal["claude", "codex", "antigravity"]
ExecutionStatus = Literal["succeeded", "failed", "timed_out", "unavailable"]
UsageCompleteness = Literal["complete", "partial", "unavailable"]
LoopStatus = Literal["succeeded", "failed", "needs_human", "budget_exhausted"]
FailureCategory = Literal[
    "none",
    "quota_exhausted",
    "rate_limit",
    "provider_overloaded",
    "context_limit",
    "timeout",
    "network",
    "authentication",
    "permission",
    "configuration",
    "schema",
    "safety",
    "unknown",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ContextItem:
    ref: str
    title: str | None = None
    body: str | None = None
    content_type: str | None = None
    source: str = "memtrace"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskEnvelope:
    task_id: str
    workspace_id: str
    goal: str
    context_refs: list[str] = field(default_factory=list)
    context_items: list[ContextItem] = field(default_factory=list)
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
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int | None = None
    cost_usd: float | None = None
    completeness: UsageCompleteness = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CliExecution:
    provider: ProviderId
    status: ExecutionStatus
    command: list[str]
    exit_code: int | None
    started_at: str
    completed_at: str
    duration_ms: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_run_id: str | None = None
    raw_trace_ref: str | None = None
    stderr_ref: str | None = None
    error: str | None = None
    parse_warnings: list[str] = field(default_factory=list)
    role_profile_id: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    reasoning_effort: str | None = None
    permission: str | None = None
    context_policy: str | None = None
    cli_version: str | None = None
    quota_bucket: str | None = None
    fallback_index: int = 0
    failure_category: FailureCategory = "none"
    retry_after: str | None = None
    reset_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["usage"] = self.usage.to_dict()
        return data


@dataclass(frozen=True)
class ModelResponse:
    adapter_id: str
    role: str
    claims: list[ModelClaim]
    execution: CliExecution
    disagreements: list[str] = field(default_factory=list)
    requires_human_decision: bool = True
    final_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["claims"] = [claim.to_dict() for claim in self.claims]
        data["execution"] = self.execution.to_dict()
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


@dataclass(frozen=True)
class LoopStageResult:
    sequence: int
    stage: str
    profile_id: str
    state: str
    response: ModelResponse
    artifact: dict[str, Any] | None = None
    attempt_index: int = 0
    fallback_from_model: str | None = None
    checkpoint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "stage": self.stage,
            "profile_id": self.profile_id,
            "state": self.state,
            "response": self.response.to_dict(),
            "artifact": self.artifact,
            "attempt_index": self.attempt_index,
            "fallback_from_model": self.fallback_from_model,
            "checkpoint_id": self.checkpoint_id,
        }


@dataclass(frozen=True)
class ResumeEnvelope:
    conversation_id: str
    run_id: str
    checkpoint_id: str
    generation: int
    reason: str
    current_stage: str
    next_action: str
    runtime_context: dict[str, Any]
    spec_context: dict[str, Any]
    loop_context: dict[str, Any]
    improvement_context: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now_iso)
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LoopSummary:
    task: TaskEnvelope
    stages: list[LoopStageResult]
    status: LoopStatus
    recommendation: str
    trace_id: str
    total_usage: TokenUsage
    run_mode: RunMode = "agent_loop"
    writeback_node_id: str | None = None
    conversation_id: str | None = None
    active_checkpoint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "stages": [stage.to_dict() for stage in self.stages],
            "status": self.status,
            "recommendation": self.recommendation,
            "trace_id": self.trace_id,
            "total_usage": self.total_usage.to_dict(),
            "run_mode": self.run_mode,
            "writeback_node_id": self.writeback_node_id,
            "conversation_id": self.conversation_id,
            "active_checkpoint_id": self.active_checkpoint_id,
        }
