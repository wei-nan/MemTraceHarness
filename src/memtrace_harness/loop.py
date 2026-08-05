from __future__ import annotations

from dataclasses import replace
import json
from typing import Any

from memtrace_harness.adapters import ModelAdapter
from memtrace_harness.continuation import build_resume_envelope, with_resume_envelope
from memtrace_harness.fallback import (
    classify_execution_failure,
    error_signature,
    permits_cross_provider_fallback,
)
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.role_profiles import RoleProfile
from memtrace_harness.schemas import (
    ContextItem,
    LoopStageResult,
    LoopStatus,
    LoopSummary,
    ModelResponse,
    TaskEnvelope,
    TokenUsage,
    CliExecution,
    utc_now_iso,
)
from memtrace_harness.trace_store import TraceStore


class AgentLoopRunner:
    """Run a bounded, fail-closed role-profile loop through local CLI adapters."""

    def __init__(
        self,
        *,
        adapters: dict[str, ModelAdapter],
        fallback_adapters: dict[str, list[ModelAdapter]] | None = None,
        role_profiles: dict[str, RoleProfile] | None = None,
        trace_store: TraceStore,
        memtrace_client: MemTraceClient | None = None,
        max_total_tokens: int | None = None,
    ) -> None:
        self.adapters = adapters
        self.fallback_adapters = fallback_adapters or {}
        self.role_profiles = role_profiles or {}
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client
        self.max_total_tokens = max_total_tokens
        self._conversation_id: str | None = None
        self._active_checkpoint_id: str | None = None
        self._validate_adapters()

    def run(
        self,
        task: TaskEnvelope,
        *,
        writeback: bool = False,
        conversation_id: str | None = None,
    ) -> LoopSummary:
        self._conversation_id = self.trace_store.create_conversation(
            task, conversation_id=conversation_id
        )
        task = replace(
            task,
            task_id=self.trace_store.conversation_task_id(self._conversation_id),
        )
        previous = self.trace_store.latest_resume_envelope(self._conversation_id)
        if previous is not None:
            task = with_resume_envelope(task, previous)
        trace_id = self.trace_store.create_run(
            task, conversation_id=self._conversation_id
        )
        self._active_checkpoint_id = previous.checkpoint_id if previous else None
        stages: list[LoopStageResult] = []

        controller = self._execute(
            task=controller_task(task, stage="start", stages=stages),
            trace_id=trace_id,
            stages=stages,
            stage="control-start",
            profile_id="controller",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, controller)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        controller_artifact = controller.artifact or {}
        if not valid_controller(controller_artifact, {"run_planner", "ask_human", "stop"}):
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller returned an invalid start decision; no stage advanced.",
                ),
                writeback=writeback,
            )
        if controller_artifact.get("action") != "run_planner":
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller did not authorize planning; keep the loop open for human review.",
                ),
                writeback=writeback,
            )

        plan = self._execute(
            task=planner_task(task),
            trace_id=trace_id,
            stages=stages,
            stage="plan",
            profile_id="planner",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, plan)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        if not valid_plan(plan.artifact):
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Sonnet did not produce a valid ready plan; no gate was advanced.",
                ),
                writeback=writeback,
            )

        g1 = self._execute(
            task=gate_task(task, "G1", plan.artifact or {}),
            trace_id=trace_id,
            stages=stages,
            stage="g1",
            profile_id="red-team",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, g1)
        if stopped:
            return self._persist(stopped, writeback=writeback)

        gate = normalized_gate(g1.artifact)
        reason_code = str((g1.artifact or {}).get("reason_code", "other"))
        if gate == "REJECT" and reason_code != "missing_input":
            use_opus = reason_code == "reasoning_gap"
            revised_plan = self._execute(
                task=(
                    planner_escalation_task(task, plan.artifact or {}, g1.artifact or {})
                    if use_opus
                    else planner_revision_task(task, plan.artifact or {}, g1.artifact or {})
                ),
                trace_id=trace_id,
                stages=stages,
                stage="plan-escalation" if use_opus else "plan-revision",
                profile_id="planner-escalation" if use_opus else "planner",
            )
            stopped = self._stop_after_execution(task, trace_id, stages, revised_plan)
            if stopped:
                return self._persist(stopped, writeback=writeback)
            if not valid_plan(revised_plan.artifact):
                planner_name = "Opus escalation" if use_opus else "Sonnet revision"
                return self._persist(
                    self._summary(
                        task,
                        trace_id,
                        stages,
                        "needs_human",
                        f"{planner_name} did not produce a valid ready plan.",
                    ),
                    writeback=writeback,
                )
            plan = revised_plan
            g1 = self._execute(
                task=gate_task(task, "G1", plan.artifact or {}),
                trace_id=trace_id,
                stages=stages,
                stage="g1-recheck",
                profile_id="red-team",
            )
            stopped = self._stop_after_execution(task, trace_id, stages, g1)
            if stopped:
                return self._persist(stopped, writeback=writeback)
            gate = normalized_gate(g1.artifact)

        if gate != "PASS":
            detail = "A red-team gate requires human review."
            if gate == "REJECT":
                detail = "Red Team rejected G1; the Harness did not start development."
            return self._persist(
                self._summary(task, trace_id, stages, "needs_human", detail),
                writeback=writeback,
            )

        development = self._execute(
            task=developer_task(task, plan.artifact or {}),
            trace_id=trace_id,
            stages=stages,
            stage="develop",
            profile_id="developer",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, development)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        if not valid_development(development.artifact):
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Gemini did not produce a complete development artifact.",
                ),
                writeback=writeback,
            )

        g2 = self._execute(
            task=gate_task(task, "G2", development.artifact or {}, plan.artifact or {}),
            trace_id=trace_id,
            stages=stages,
            stage="g2",
            profile_id="red-team",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, g2)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        g2_verdict = normalized_gate(g2.artifact)
        g2_reason = str((g2.artifact or {}).get("reason_code", "other"))
        if g2_verdict == "REJECT" and g2_reason != "missing_input":
            development = self._execute(
                task=developer_revision_task(
                    task,
                    plan.artifact or {},
                    development.artifact or {},
                    g2.artifact or {},
                ),
                trace_id=trace_id,
                stages=stages,
                stage="develop-revision",
                profile_id="developer",
            )
            stopped = self._stop_after_execution(task, trace_id, stages, development)
            if stopped:
                return self._persist(stopped, writeback=writeback)
            if not valid_development(development.artifact):
                return self._persist(
                    self._summary(
                        task,
                        trace_id,
                        stages,
                        "needs_human",
                        "Gemini revision did not produce a complete development artifact.",
                    ),
                    writeback=writeback,
                )
            g2 = self._execute(
                task=gate_task(
                    task, "G2", development.artifact or {}, plan.artifact or {}
                ),
                trace_id=trace_id,
                stages=stages,
                stage="g2-recheck",
                profile_id="red-team",
            )
            stopped = self._stop_after_execution(task, trace_id, stages, g2)
            if stopped:
                return self._persist(stopped, writeback=writeback)
            g2_verdict = normalized_gate(g2.artifact)

        if g2_verdict != "PASS":
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Red Team did not pass G2; the result remains draft evidence.",
                ),
                writeback=writeback,
            )

        converge = self._execute(
            task=controller_task(task, stage="converge", stages=stages),
            trace_id=trace_id,
            stages=stages,
            stage="converge",
            profile_id="controller",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, converge)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        if not valid_controller(converge.artifact, {"finish", "ask_human"}):
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller returned an invalid convergence decision.",
                ),
                writeback=writeback,
            )
        if (converge.artifact or {}).get("action") != "finish":
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller did not converge the loop; keep it open for human review.",
                ),
                writeback=writeback,
            )
        return self._persist(
            self._summary(
                task,
                trace_id,
                stages,
                "succeeded",
                (
                    "The bounded local loop completed with draft evidence. This does not assert "
                    "a MemTrace server-side gate or task-state transition."
                ),
            ),
            writeback=writeback,
        )

    def _execute(
        self,
        *,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        stage: str,
        profile_id: str,
    ) -> LoopStageResult:
        candidates = [
            self.adapters[profile_id],
            *self.fallback_adapters.get(profile_id, []),
        ]
        fallback_from_model: str | None = None
        effective_task = task
        last_result: LoopStageResult | None = None

        for attempt_index, adapter in enumerate(candidates):
            quota_bucket = _adapter_quota_bucket(adapter)
            if not self.trace_store.quota_bucket_available(quota_bucket):
                last_result = self._record_quota_cooldown(
                    task=effective_task,
                    trace_id=trace_id,
                    stages=stages,
                    stage=stage,
                    profile_id=profile_id,
                    adapter=adapter,
                    attempt_index=attempt_index,
                    fallback_from_model=fallback_from_model,
                    quota_bucket=quota_bucket,
                )
                fallback_from_model = str(
                    getattr(adapter, "model", None) or "provider-default"
                )
                continue
            if attempt_index > 0:
                envelope = self._checkpoint(
                    task=effective_task,
                    trace_id=trace_id,
                    stages=stages,
                    stage=stage,
                    reason="provider_switch",
                    next_action=(
                        f"Retry {profile_id} stage with provider/model "
                        f"{getattr(adapter, 'provider', 'unknown')}/"
                        f"{getattr(adapter, 'model', 'provider-default')}"
                    ),
                )
                effective_task = with_resume_envelope(task, envelope)

            sequence = len(stages) + 1
            response = adapter.run(
                effective_task,
                f"{trace_id}/{sequence:02d}_{stage}/attempt_{attempt_index}",
            )
            category = classify_execution_failure(response.execution)
            execution = replace(
                response.execution,
                role_profile_id=profile_id,
                quota_bucket=quota_bucket,
                fallback_index=attempt_index,
                failure_category=category,
            )
            response = replace(response, execution=execution)
            artifact = (
                parse_json_object(response.final_text)
                if response.execution.status == "succeeded"
                else None
            )
            state = response.execution.status
            if response.execution.status == "succeeded" and artifact is None:
                state = "invalid_output"
                execution = replace(response.execution, failure_category="schema")
                response = replace(response, execution=execution)

            checkpoint_id, _ = self.trace_store.next_checkpoint_identity(
                self._required_conversation_id()
            )
            result = LoopStageResult(
                sequence=sequence,
                stage=stage,
                profile_id=profile_id,
                state=state,
                response=response,
                artifact=artifact,
                attempt_index=attempt_index,
                fallback_from_model=fallback_from_model,
                checkpoint_id=checkpoint_id,
            )
            stages.append(result)
            self.trace_store.save_stage_result(
                conversation_id=self._required_conversation_id(),
                run_id=trace_id,
                result=result,
            )
            envelope = self._checkpoint(
                task=effective_task,
                trace_id=trace_id,
                stages=stages,
                stage=stage,
                reason="stage_boundary",
                next_action=(
                    "Advance only after validating this stage artifact."
                    if state == "succeeded"
                    else "Apply the explicit failure policy before another provider call."
                ),
                checkpoint_id=checkpoint_id,
            )
            self.trace_store.record_provider_availability(
                provider=response.execution.provider,
                model=response.execution.resolved_model
                or response.execution.requested_model,
                quota_bucket=quota_bucket,
                failure_category=response.execution.failure_category,
                error_signature=error_signature(response.execution),
                cooldown_seconds=self._fallback_cooldown_seconds(profile_id),
            )
            self._active_checkpoint_id = envelope.checkpoint_id
            last_result = result

            if response.execution.status == "succeeded":
                return result
            if not self._may_fallback(
                profile_id=profile_id,
                category=response.execution.failure_category,
                attempt_index=attempt_index,
                candidates=candidates,
                stages=stages,
            ):
                return result
            fallback_from_model = (
                response.execution.resolved_model or response.execution.requested_model
            )

        if last_result is None:
            raise RuntimeError(f"No available execution candidate for profile {profile_id}")
        return last_result

    def _record_quota_cooldown(
        self,
        *,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        stage: str,
        profile_id: str,
        adapter: ModelAdapter,
        attempt_index: int,
        fallback_from_model: str | None,
        quota_bucket: str,
    ) -> LoopStageResult:
        now = utc_now_iso()
        checkpoint_id, _ = self.trace_store.next_checkpoint_identity(
            self._required_conversation_id()
        )
        execution = CliExecution(
            provider=getattr(adapter, "provider", "codex"),
            status="unavailable",
            command=[],
            exit_code=None,
            started_at=now,
            completed_at=now,
            duration_ms=0,
            provider_run_id=None,
            error=f"Quota bucket {quota_bucket!r} is in cooldown; CLI was not invoked.",
            usage=TokenUsage(total_tokens=0, completeness="complete"),
            role_profile_id=profile_id,
            requested_model=getattr(adapter, "model", None),
            reasoning_effort=getattr(adapter, "reasoning_effort", None),
            permission=getattr(adapter, "permission", None),
            context_policy=getattr(adapter, "context_policy", None),
            cli_version=getattr(adapter, "cli_version", None),
            quota_bucket=quota_bucket,
            fallback_index=attempt_index,
            failure_category="quota_exhausted",
        )
        response = ModelResponse(
            adapter_id=str(getattr(adapter, "adapter_id", profile_id)),
            role=str(getattr(adapter, "role", profile_id)),
            claims=[],
            execution=execution,
            requires_human_decision=True,
            final_text="",
        )
        result = LoopStageResult(
            sequence=len(stages) + 1,
            stage=stage,
            profile_id=profile_id,
            state="quota_cooldown",
            response=response,
            attempt_index=attempt_index,
            fallback_from_model=fallback_from_model,
            checkpoint_id=checkpoint_id,
        )
        stages.append(result)
        self.trace_store.save_stage_result(
            conversation_id=self._required_conversation_id(),
            run_id=trace_id,
            result=result,
        )
        self._checkpoint(
            task=task,
            trace_id=trace_id,
            stages=stages,
            stage=stage,
            reason="quota_cooldown",
            next_action="Use an approved candidate from a different quota bucket or pause.",
            checkpoint_id=checkpoint_id,
        )
        return result

    def _checkpoint(
        self,
        *,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        stage: str,
        reason: str,
        next_action: str,
        checkpoint_id: str | None = None,
    ):
        identity, generation = self.trace_store.next_checkpoint_identity(
            self._required_conversation_id()
        )
        if checkpoint_id is not None:
            identity = checkpoint_id
        envelope = build_resume_envelope(
            task=task,
            conversation_id=self._required_conversation_id(),
            run_id=trace_id,
            checkpoint_id=identity,
            generation=generation,
            reason=reason,
            current_stage=stage,
            next_action=next_action,
            stages=stages,
        )
        self.trace_store.save_checkpoint(envelope)
        self._active_checkpoint_id = envelope.checkpoint_id
        return envelope

    def _may_fallback(
        self,
        *,
        profile_id: str,
        category,
        attempt_index: int,
        candidates: list[ModelAdapter],
        stages: list[LoopStageResult],
    ) -> bool:
        if attempt_index + 1 >= len(candidates):
            return False
        profile = self.role_profiles.get(profile_id)
        if profile is not None and category not in profile.fallback_on:
            return False
        if not permits_cross_provider_fallback(category):
            return False
        if self.max_total_tokens is not None and any(
            item.response.execution.usage.completeness == "unavailable"
            for item in stages
        ):
            return False
        return True

    def _fallback_cooldown_seconds(self, profile_id: str) -> int:
        profile = self.role_profiles.get(profile_id)
        return profile.fallback_cooldown_seconds if profile else 1800

    def _required_conversation_id(self) -> str:
        if self._conversation_id is None:
            raise RuntimeError("Agent loop has no active conversation")
        return self._conversation_id

    def _stop_after_execution(
        self,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        result: LoopStageResult,
    ) -> LoopSummary | None:
        if self.max_total_tokens is not None:
            if any(
                stage.response.execution.usage.completeness == "unavailable"
                for stage in stages
            ):
                return self._summary(
                    task,
                    trace_id,
                    stages,
                    "budget_exhausted",
                    "Token usage became unavailable under a hard budget; the loop stopped closed.",
                )
            spent = token_budget_units(stages)
            if spent >= self.max_total_tokens:
                return self._summary(
                    task,
                    trace_id,
                    stages,
                    "budget_exhausted",
                    (
                        f"The loop reached its token budget ({spent} >= "
                        f"{self.max_total_tokens}) at a stage boundary."
                    ),
                )
        if result.response.execution.status != "succeeded":
            return self._summary(
                task,
                trace_id,
                stages,
                "failed",
                f"{result.profile_id} CLI execution did not succeed; the loop stopped.",
            )
        if result.artifact is None:
            return self._summary(
                task,
                trace_id,
                stages,
                "needs_human",
                f"{result.profile_id} returned invalid structured output; no stage advanced.",
            )
        return None

    def _summary(
        self,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        status: LoopStatus,
        recommendation: str,
    ) -> LoopSummary:
        return LoopSummary(
            task=task,
            stages=list(stages),
            status=status,
            recommendation=recommendation,
            trace_id=trace_id,
            total_usage=aggregate_usage(stages),
            conversation_id=self._conversation_id,
            active_checkpoint_id=self._active_checkpoint_id,
        )

    def _persist(self, summary: LoopSummary, *, writeback: bool) -> LoopSummary:
        self.trace_store.save_loop_summary(summary)
        if writeback:
            if not self.memtrace_client:
                raise RuntimeError("writeback requested, but no MemTrace client is configured")
            node_id = self.memtrace_client.create_node(
                workspace_id=summary.task.workspace_id,
                title=f"Harness loop draft: {summary.task.goal[:64]}",
                body=render_loop_memtrace_draft(summary),
                content_type="inquiry",
            )
            summary = replace(
                summary,
                run_mode="agent_loop_draft_write",
                writeback_node_id=node_id,
            )
            self.trace_store.update_run_summary(summary)
        return summary

    def _validate_adapters(self) -> None:
        required = {
            "controller",
            "planner",
            "planner-escalation",
            "red-team",
            "developer",
        }
        missing = sorted(required.difference(self.adapters))
        if missing:
            raise ValueError(f"Agent loop is missing role adapters: {', '.join(missing)}")


def controller_task(
    task: TaskEnvelope, *, stage: str, stages: list[LoopStageResult]
) -> TaskEnvelope:
    snapshot = {
        "task_id": task.task_id,
        "stage": stage,
        "risk_level": task.risk_level,
        "context_refs": task.context_refs,
        "completed_stages": [
            {
                "stage": item.stage,
                "profile_id": item.profile_id,
                "state": item.state,
                "artifact": artifact_snapshot(item.artifact),
            }
            for item in stages
        ],
        "token_spent": token_budget_units(stages),
    }
    allowed = ["run_planner", "ask_human", "stop"] if stage == "start" else [
        "finish",
        "ask_human",
    ]
    return stage_task(
        task,
        suffix=f"controller-{stage}",
        goal=(
            f"Act as the bounded loop controller for {stage}. Original goal: {task.goal}\n"
            f"Return only a valid JSON object with action in {allowed!r} and a string reason."
        ),
        context_items=[
            ContextItem(
                ref=f"harness:loop-snapshot:{stage}",
                title="Loop snapshot",
                body=json.dumps(snapshot, ensure_ascii=False),
                content_type="loop_snapshot",
                source="harness",
            ),
            *[
                item
                for item in task.context_items
                if item.content_type == "harness_resume_envelope"
            ],
        ],
    )


def planner_task(task: TaskEnvelope) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan",
        goal=(
            f"Plan this goal with Sonnet regardless of risk level: {task.goal}\n"
            "Return only a valid JSON object with status ('ready' or 'needs_human'), plan "
            "(string), acceptance_criteria (string array), open_questions (string array), "
            "and scope_exclusions (string array). "
            "Do not make product decisions when required input is missing."
        ),
        context_items=task.context_items,
    )


def planner_escalation_task(
    task: TaskEnvelope, plan: dict[str, Any], gate: dict[str, Any]
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan-escalation",
        goal=(
            f"Revise the plan only because G1 identified a reasoning gap: {task.goal}\n"
            "Return the same JSON plan contract as the standard planner."
        ),
        context_items=[
            artifact_item("harness:sonnet-plan", "Rejected Sonnet plan", plan),
            artifact_item("harness:g1-reject", "G1 rejection", gate),
        ],
    )


def planner_revision_task(
    task: TaskEnvelope, plan: dict[str, Any], gate: dict[str, Any]
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan-revision",
        goal=(
            f"Revise the Sonnet plan using the first G1 rejection: {task.goal}\n"
            "Return the same valid JSON plan contract. This is one bounded correction, not a retry."
        ),
        context_items=[
            artifact_item("harness:sonnet-plan", "Rejected Sonnet plan", plan),
            artifact_item("harness:g1-reject", "G1 rejection", gate),
        ],
    )


def gate_task(
    task: TaskEnvelope,
    gate_name: str,
    evidence: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> TaskEnvelope:
    items = [artifact_item(f"harness:{gate_name.lower()}-evidence", "Gate evidence", evidence)]
    if plan is not None:
        items.append(artifact_item("harness:accepted-plan", "Accepted plan", plan))
    return stage_task(
        task,
        suffix=gate_name.lower(),
        goal=(
            f"Red-team {gate_name} for: {task.goal}\n"
            "Return only a valid JSON object with verdict ('PASS', 'REJECT', or "
            "'NEEDS_HUMAN'), reason_code ('none', 'reasoning_gap', 'missing_input', "
            "'acceptance_gap', 'security', 'scope_drift', 'implementation_gap', "
            "'test_gap', or 'other'), findings (object array), unverified_items "
            "(string array), and confidence (number). Do not edit files."
        ),
        context_items=items,
    )


def developer_task(task: TaskEnvelope, plan: dict[str, Any]) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="develop",
        goal=(
            f"Implement the accepted plan for: {task.goal}\n"
            "Return only a valid JSON object after the work with status ('completed', "
            "'needs_human', or 'failed'), summary (string), changed_files (string array), "
            "tests (string array), and gaps (string array)."
        ),
        context_items=[
            *task.context_items,
            artifact_item("harness:accepted-plan", "Accepted plan", plan),
        ],
    )


def developer_revision_task(
    task: TaskEnvelope,
    plan: dict[str, Any],
    development: dict[str, Any],
    gate: dict[str, Any],
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="develop-revision",
        goal=(
            f"Correct the implementation using the first G2 rejection: {task.goal}\n"
            "Return the same valid JSON development contract after running relevant tests. "
            "This is one bounded correction, not an unchanged retry."
        ),
        context_items=[
            artifact_item("harness:accepted-plan", "Accepted plan", plan),
            artifact_item("harness:development", "Rejected development result", development),
            artifact_item("harness:g2-reject", "G2 rejection", gate),
        ],
    )


def stage_task(
    task: TaskEnvelope,
    *,
    suffix: str,
    goal: str,
    context_items: list[ContextItem],
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=f"{task.task_id}:{suffix}",
        workspace_id=task.workspace_id,
        goal=goal,
        context_refs=task.context_refs,
        context_items=context_items,
        constraints=task.constraints,
        done_when=task.done_when,
        risk_level=task.risk_level,
        source="harness-agent-loop",
    )


def artifact_item(ref: str, title: str, value: dict[str, Any]) -> ContextItem:
    return ContextItem(
        ref=ref,
        title=title,
        body=json.dumps(value, ensure_ascii=False),
        content_type="harness_artifact",
        source="harness",
    )


def artifact_snapshot(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    snapshot: dict[str, Any] = {}
    for key in ("action", "status", "verdict", "reason", "reason_code", "summary"):
        item = value.get(key)
        if isinstance(item, str):
            snapshot[key] = item[:500]
    for key in (
        "acceptance_criteria",
        "open_questions",
        "findings",
        "unverified_items",
        "changed_files",
        "tests",
        "gaps",
    ):
        item = value.get(key)
        if isinstance(item, list):
            snapshot[f"{key}_count"] = len(item)
    return snapshot


def parse_json_object(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if len(candidate) > 256_000:
        return None
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def valid_plan(value: dict[str, Any] | None) -> bool:
    if not value or value.get("status") != "ready":
        return False
    plan = value.get("plan")
    return (
        isinstance(plan, str)
        and bool(plan.strip())
        and len(plan) <= 12_000
        and valid_string_list(value.get("acceptance_criteria"), 50, 1_000)
        and valid_string_list(value.get("open_questions"), 20, 1_000)
        and valid_string_list(value.get("scope_exclusions"), 30, 1_000)
    )


def valid_development(value: dict[str, Any] | None) -> bool:
    if not value or value.get("status") != "completed":
        return False
    summary = value.get("summary")
    return (
        isinstance(summary, str)
        and bool(summary.strip())
        and len(summary) <= 6_000
        and valid_string_list(value.get("changed_files"), 200, 1_000)
        and valid_string_list(value.get("tests"), 200, 2_000)
        and valid_string_list(value.get("gaps"), 50, 2_000)
    )


def normalized_gate(value: dict[str, Any] | None) -> str:
    if not value:
        return "INVALID"
    verdict = str(value.get("verdict", "")).upper()
    reason_codes = {
        "none",
        "reasoning_gap",
        "missing_input",
        "acceptance_gap",
        "security",
        "scope_drift",
        "implementation_gap",
        "test_gap",
        "other",
    }
    findings = value.get("findings")
    confidence = value.get("confidence")
    if (
        verdict not in {"PASS", "REJECT", "NEEDS_HUMAN"}
        or value.get("reason_code") not in reason_codes
        or not isinstance(findings, list)
        or len(findings) > 50
        or not all(valid_finding(item) for item in findings)
        or not valid_string_list(value.get("unverified_items"), 50, 1_000)
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        return "INVALID"
    return verdict


def valid_controller(value: dict[str, Any] | None, allowed: set[str]) -> bool:
    if not value or value.get("action") not in allowed:
        return False
    reason = value.get("reason")
    return isinstance(reason, str) and len(reason) <= 1_000


def valid_string_list(value: Any, max_items: int, max_length: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= max_items
        and all(isinstance(item, str) and len(item) <= max_length for item in value)
    )


def valid_finding(value: Any) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("description"), str):
        return False
    allowed = {
        "severity": 100,
        "description": 2_000,
        "evidence_ref": 1_000,
        "requirement_ref": 1_000,
        "required_action": 2_000,
    }
    if set(value).difference(allowed):
        return False
    return all(
        isinstance(item, str) and len(item) <= allowed[key]
        for key, item in value.items()
    )


def token_budget_units(stages: list[LoopStageResult]) -> int:
    total = 0
    for stage in stages:
        usage = stage.response.execution.usage
        total += (
            usage.total_tokens
            if usage.total_tokens is not None
            else usage.input_tokens + usage.output_tokens
        )
    return total


def _adapter_quota_bucket(adapter: ModelAdapter) -> str:
    value = getattr(adapter, "quota_bucket", None)
    if isinstance(value, str) and value.strip():
        return value
    provider = str(getattr(adapter, "provider", "unknown"))
    model = str(getattr(adapter, "model", "provider-default"))
    return f"{provider}:{model}"


def aggregate_usage(stages: list[LoopStageResult]) -> TokenUsage:
    usages = [stage.response.execution.usage for stage in stages]
    if not usages:
        return TokenUsage()
    completeness = (
        "complete"
        if all(item.completeness == "complete" for item in usages)
        else "unavailable"
        if all(item.completeness == "unavailable" for item in usages)
        else "partial"
    )
    total_values = [item.total_tokens for item in usages]
    cost_values = [item.cost_usd for item in usages]
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in usages),
        cached_input_tokens=sum(item.cached_input_tokens for item in usages),
        cache_creation_input_tokens=sum(item.cache_creation_input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        reasoning_output_tokens=sum(item.reasoning_output_tokens for item in usages),
        total_tokens=(
            sum(value for value in total_values if value is not None)
            if all(value is not None for value in total_values)
            else None
        ),
        cost_usd=(
            sum(value for value in cost_values if value is not None)
            if all(value is not None for value in cost_values)
            else None
        ),
        completeness=completeness,
    )


def render_loop_memtrace_draft(summary: LoopSummary) -> str:
    lines = [
        "## Harness Agent Loop Draft",
        "",
        f"- Trace: `{summary.trace_id}`",
        f"- Conversation: `{summary.conversation_id or 'unavailable'}`",
        f"- Active checkpoint: `{summary.active_checkpoint_id or 'unavailable'}`",
        f"- Local status: `{summary.status}`",
        f"- Token completeness: `{summary.total_usage.completeness}`",
        f"- Input/output tokens: `{summary.total_usage.input_tokens}` / "
        f"`{summary.total_usage.output_tokens}`",
        "",
        "## Stage Evidence",
        "",
    ]
    for stage in summary.stages:
        execution = stage.response.execution
        lines.append(
            f"- {stage.sequence}. `{stage.stage}` via `{stage.profile_id}` "
            f"({execution.resolved_model or execution.requested_model or 'provider-default'}; "
            f"fallback={stage.attempt_index}; failure={execution.failure_category}): "
            f"`{stage.state}`; "
            f"trace `{execution.raw_trace_ref or 'unavailable'}`"
        )
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            summary.recommendation,
            "",
            (
                "Draft-only Harness evidence. This node does not assert a MemTrace DB gate, "
                "blocked, gate-rejected, reject-count, or completed transition."
            ),
        ]
    )
    return "\n".join(lines)
