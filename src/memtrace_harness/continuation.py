from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from memtrace_harness.schemas import (
    ContextItem,
    LoopStageResult,
    ResumeEnvelope,
    TaskEnvelope,
)


MAX_CHECKPOINT_ATTEMPTS = 20
MAX_ARTIFACT_TEXT = 2_000
MAX_RESUME_ENVELOPE_CHARS = 256_000


def build_resume_envelope(
    *,
    task: TaskEnvelope,
    conversation_id: str,
    run_id: str,
    checkpoint_id: str,
    generation: int,
    reason: str,
    current_stage: str,
    next_action: str,
    stages: list[LoopStageResult],
) -> ResumeEnvelope:
    attempts = [_attempt_snapshot(item) for item in stages[-MAX_CHECKPOINT_ATTEMPTS:]]
    return ResumeEnvelope(
        conversation_id=conversation_id,
        run_id=run_id,
        checkpoint_id=checkpoint_id,
        generation=generation,
        reason=reason,
        current_stage=current_stage,
        next_action=next_action,
        runtime_context={
            "objective": task.goal,
            "risk_level": task.risk_level,
            "attempts": attempts,
            "token_spent": _token_units(stages),
            "usage_completeness": _usage_completeness(stages),
        },
        spec_context={
            "workspace_id": task.workspace_id,
            "context_refs": list(task.context_refs[:50]),
            "acceptance_criteria": list(task.done_when[:50]),
        },
        loop_context={
            "task_id": task.task_id,
            "stage": current_stage,
            "constraints": list(task.constraints[:50]),
        },
        improvement_context=None,
    )


def with_resume_envelope(task: TaskEnvelope, envelope: ResumeEnvelope) -> TaskEnvelope:
    payload = json.dumps(envelope.to_dict(), ensure_ascii=False, separators=(",", ":"))
    if len(payload) > MAX_RESUME_ENVELOPE_CHARS:
        raise ValueError(
            "ResumeEnvelope exceeds its bounded context contract; compact source artifacts first"
        )
    item = ContextItem(
        ref=f"harness:checkpoint:{envelope.checkpoint_id}",
        title="Harness resume envelope",
        body=payload,
        content_type="harness_resume_envelope",
        source="harness",
    )
    retained = [
        context
        for context in task.context_items
        if context.content_type != "harness_resume_envelope"
    ]
    return replace(task, context_items=[*retained, item])


def _attempt_snapshot(stage: LoopStageResult) -> dict[str, Any]:
    execution = stage.response.execution
    return {
        "sequence": stage.sequence,
        "stage": stage.stage,
        "profile_id": stage.profile_id,
        "attempt_index": stage.attempt_index,
        "state": stage.state,
        "provider": execution.provider,
        "model": execution.resolved_model or execution.requested_model,
        "provider_session_id": execution.provider_run_id,
        "failure_category": execution.failure_category,
        "usage": execution.usage.to_dict(),
        "artifact": _bounded_artifact(stage.artifact),
        "raw_trace_ref": execution.raw_trace_ref,
    }


def _bounded_artifact(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    bounded = _bounded_value(value, depth=0)
    return bounded if isinstance(bounded, dict) else None


def _bounded_value(value: Any, *, depth: int) -> Any:
    if depth >= 4:
        return "[truncated]"
    if isinstance(value, str):
        return value[:MAX_ARTIFACT_TEXT]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_bounded_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _bounded_value(item, depth=depth + 1)
            for key, item in list(value.items())[:50]
        }
    return str(value)[:MAX_ARTIFACT_TEXT]


def _token_units(stages: list[LoopStageResult]) -> int:
    total = 0
    for stage in stages:
        usage = stage.response.execution.usage
        total += (
            usage.total_tokens
            if usage.total_tokens is not None
            else usage.input_tokens + usage.output_tokens
        )
    return total


def _usage_completeness(stages: list[LoopStageResult]) -> str:
    values = [stage.response.execution.usage.completeness for stage in stages]
    if not values or all(value == "unavailable" for value in values):
        return "unavailable"
    if all(value == "complete" for value in values):
        return "complete"
    return "partial"
