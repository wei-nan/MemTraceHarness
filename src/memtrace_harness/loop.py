from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
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
        approval_manager: Any | None = None,
        working_directory: Path | None = None,
    ) -> None:
        self.adapters = adapters
        self.fallback_adapters = fallback_adapters or {}
        self.role_profiles = role_profiles or {}
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client
        self.max_total_tokens = max_total_tokens
        self.approval_manager = approval_manager
        self.working_directory = working_directory
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
        # A resumed conversation otherwise always restarts at Controller and
        # re-executes every stage, even when this exact conversation already has a
        # still-usable artifact for a later stage sitting in trace_store from an
        # earlier run() call. Seed `stages` with the latest skip-worthy artifact per
        # stage family so the loop resumes at the actual point of interruption
        # instead of redoing settled work (and re-spending quota on it) on every
        # human answer.
        reconstructed = self._reconstruct_progress(self._conversation_id)
        stages.extend(reconstructed.values())

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
                    summarize_invalid_artifact("Controller", controller_artifact),
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
                    "Controller chose not to proceed: "
                    + str(controller_artifact.get("reason") or "(no reason given)"),
                ),
                writeback=writeback,
            )

        reused_plan = reconstructed.get("plan")
        if reused_plan is not None:
            plan = reused_plan
        else:
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
                        summarize_invalid_artifact("Planner", plan.artifact),
                    ),
                    writeback=writeback,
                )

        reused_g1 = reconstructed.get("g1")
        if reused_g1 is not None:
            g1 = reused_g1
            gate = "PASS"
        else:
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
                            summarize_invalid_artifact(planner_name, revised_plan.artifact),
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
                detail = summarize_gate_artifact("G1", g1.artifact)
                return self._persist(
                    self._summary(task, trace_id, stages, "needs_human", detail),
                    writeback=writeback,
                )

        if "git push" in task.goal.lower() and self.approval_manager:
            pending = self.approval_manager.get_pending_for_conversation(self._required_conversation_id())
            if not pending or pending.reason != "git_push" or pending.status != "approved":
                self.approval_manager.request_approval(
                    conversation_id=self._required_conversation_id(),
                    workspace=task.workspace_id,
                    working_directory=str(self.working_directory or ""),
                    reason="git_push",
                    proposed_action=f"Execute git push for task: {task.goal[:80]}",
                    resume_goal=task.goal,
                )
                return self._persist(
                    self._summary(
                        task,
                        trace_id,
                        stages,
                        "needs_human",
                        "git push is a gated remote action requiring explicit out-of-band human approval.",
                    ),
                    writeback=writeback,
                )

        reused_develop = reconstructed.get("develop")
        if reused_develop is not None:
            development = reused_develop
        else:
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
            if normalized_development(development.artifact) == "INVALID":
                return self._persist(
                    self._summary(
                        task,
                        trace_id,
                        stages,
                        "needs_human",
                        summarize_invalid_artifact("Developer", development.artifact),
                    ),
                    writeback=writeback,
                )
        # A developer-reported "needs_human"/"failed" status is not itself a reason to
        # stop and page a human — G2 is the reviewer built to catch exactly the gaps a
        # developer would flag (missing compile evidence, self-expanded scope, etc.)
        # and, via its own REJECT verdict, drive the same one-bounded-revision loop it
        # already runs for a "completed" development. Only G2's own verdict (or the
        # INVALID case above) should end this without a human.

        reused_g2 = reconstructed.get("g2")
        if reused_g2 is not None:
            g2 = reused_g2
            g2_verdict = "PASS"
        else:
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
                if normalized_development(development.artifact) == "INVALID":
                    return self._persist(
                        self._summary(
                            task,
                            trace_id,
                            stages,
                            "needs_human",
                            summarize_invalid_artifact(
                                "Developer revision", development.artifact
                            ),
                        ),
                        writeback=writeback,
                    )
                # Same reasoning as the first Developer stage above: a "needs_human"/
                # "failed" revision still goes to the g2-recheck below rather than an
                # immediate human stop — the recheck's own verdict is what's final here.
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
                        summarize_gate_artifact("G2", g2.artifact),
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
                    summarize_invalid_artifact("Controller (converge)", converge.artifact),
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
                    "Controller chose not to finish: "
                    + str((converge.artifact or {}).get("reason") or "(no reason given)"),
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

    def _reconstruct_progress(self, conversation_id: str) -> dict[str, LoopStageResult]:
        """Scan this conversation's full turn history (across every prior run() call,
        not just this one) for the latest skip-worthy artifact per stage family — see
        _skip_worthy(). Returns a synthetic, unpersisted LoopStageResult per family
        (state="reused") built from trace_store data alone, without invoking any
        adapter; run() uses these to bypass re-executing stages that already reached a
        genuinely reusable outcome."""
        found: dict[str, LoopStageResult] = {}
        for row in self.trace_store.get_conversation_pipeline(conversation_id):
            if row.get("state") != "succeeded":
                continue
            family = next(
                (name for name, names in _STAGE_FAMILIES.items() if row["stage"] in names),
                None,
            )
            if family is None:
                continue
            detail = self.trace_store.get_turn_detail(row["turn_id"])
            artifact = detail.get("artifact") if detail else None
            if not _skip_worthy(family, artifact):
                continue
            created_at = row.get("created_at") or utc_now_iso()
            execution = CliExecution(
                provider=str(row.get("provider") or "unknown"),
                status="succeeded",
                command=[],
                exit_code=0,
                started_at=created_at,
                completed_at=created_at,
                duration_ms=0,
                role_profile_id=row.get("profile_id"),
                requested_model=row.get("model"),
                resolved_model=row.get("model"),
            )
            response = ModelResponse(
                adapter_id=str(row.get("profile_id")),
                role=str(row.get("profile_id")),
                claims=[],
                execution=execution,
                requires_human_decision=False,
                final_text=json.dumps(artifact, ensure_ascii=False) if artifact else "",
            )
            found[family] = LoopStageResult(
                sequence=int(row.get("sequence") or 0),
                stage=f"{row['stage']}-reused",
                profile_id=str(row.get("profile_id")),
                state="reused",
                response=response,
                artifact=artifact,
                attempt_index=int(row.get("attempt_index") or 0),
            )
        return found

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
            error_detail = result.response.execution.error
            detail = f"{result.profile_id} CLI execution did not succeed; the loop stopped."
            if error_detail:
                detail += f"\nError: {str(error_detail)[:300]}"
            return self._summary(task, trace_id, stages, "failed", detail)
        if result.artifact is None:
            # No parseable JSON — the best a human can act on here is what the model
            # actually said, not just "it was invalid" (this is exactly the shape of
            # the 2026-08-12 Claude plan-mode diversion bug: the model's real content
            # existed, just wasn't in a form the loop could parse as its verdict).
            raw_snippet = (result.response.final_text or "").strip()[:400]
            detail = f"{result.profile_id} returned invalid structured output; no stage advanced."
            if raw_snippet:
                detail += f"\nModel said:\n{raw_snippet}"
            return self._summary(task, trace_id, stages, "needs_human", detail)
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
        if summary.status in {"needs_human", "budget_exhausted"} and self.approval_manager:
            reason = "budget_exhausted" if summary.status == "budget_exhausted" else "ambiguous_requirement"
            self.approval_manager.request_approval(
                conversation_id=summary.conversation_id or self._required_conversation_id(),
                workspace=summary.task.workspace_id,
                working_directory=str(self.working_directory or ""),
                reason=reason,
                proposed_action=summary.recommendation,
                # proposed_action is shown to the human as *why it stopped*; resume_goal
                # is what a later /approve should actually resume with. Without this
                # split, approving would re-run with the failure summary as the new
                # goal instead of continuing the human's original request.
                resume_goal=summary.task.goal,
            )
        if writeback:
            if not self.memtrace_client:
                raise RuntimeError("writeback requested, but no MemTrace client is configured")
            # force_create=True: each loop stop is its own draft record of that
            # specific run's state (stages, verdicts, usage) — a resumed conversation
            # legitimately produces several of these over time with a near-identical
            # title/goal prefix, which is exactly what MemTrace's similarity-based
            # duplicate detection flags. That's correct behavior for the general case
            # (accidental re-submission of the same content) but wrong here, where
            # recurrence is expected and each record needs to be kept, not merged.
            node_id = self.memtrace_client.create_node(
                workspace_id=summary.task.workspace_id,
                title=f"Harness loop draft: {summary.task.goal[:64]}",
                body=render_loop_memtrace_draft(summary),
                content_type="inquiry",
                force_create=True,
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
            "You are deliberately running in a neutral, empty sandbox directory, not the "
            "project's actual repository — this is intentional so this stage stays cheap "
            "and fast, not a malfunction. Any working_directory/path mentioned in the goal "
            "text above is informational context for later stages (Planner/Developer, "
            "which do have real repo access), not something you can or should verify "
            "yourself.\n"
            "Do not call any tool for any reason — no file read, no directory listing, no "
            "git command, no web search, no browser, nothing. This explicitly includes "
            "AGENTS.md, CLAUDE.md, an \"operating contract\", architecture docs, or any "
            "other convention you might normally check first: none of that exists in this "
            "sandbox, none of it is needed for this decision, and attempting to read it "
            "will only fail (permission denied — the sandbox is intentionally outside any "
            "granted directory) and waste the whole turn. This is a single-turn, "
            "zero-tool-call decision: answer directly from the task envelope's goal and "
            "loop-snapshot context below, nothing else. If any tool call you attempt is "
            "denied or errors, do not stop or explain what happened — immediately answer "
            "the JSON decision anyway using whatever you already have; a partial answer is "
            "always better than none.\n"
            "The JSON schema enforced on this call's output is shared by both controller "
            "stages (start and converge) and therefore lists a wider action enum than is "
            "valid right now — it will not stop you from picking a value that's wrong for "
            f"this specific stage ({stage!r}). The ONLY actions valid at this stage are "
            f"{allowed!r} — picking any other enum member (even though the schema permits "
            "it) is a mistake, not a valid choice, regardless of what your reasoning says.\n"
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


# Shared across every stage that can use tools (Planner, Red Team, Developer — not
# Controller, which has its own stronger "use zero tools at all" instruction since it
# runs in an empty sandbox). Confirmed 2026-08-14 on a real Red Team run: a denied
# `run_command` (git status) call — not a quota/schema issue, a plain permission
# denial — caused the model to end its turn with only prose explaining what it was
# doing, never producing the required JSON at all. Same root cause as the Controller
# fix earlier the same day (adapters/antigravity.py's accept-edits switch means some
# tool calls can still be denied by the CLI's own remaining permission layer), just
# not yet applied outside Controller. A denial is recoverable information (something
# to note in findings/gaps/confidence), not a reason to abandon the turn.
_TOOL_DENIAL_RESILIENCE = (
    "If any tool call you attempt (file read, shell command, MCP call, or anything "
    "else) is denied, errors, or times out, do not stop or explain what happened "
    "instead of answering — immediately produce the required JSON using whatever "
    "you already have. Note what you couldn't verify via unverified_items/"
    "open_questions/gaps and a correspondingly lower confidence, as appropriate — "
    "but always end the turn with the structured JSON, never with prose alone."
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
            "Do not make product decisions when required input is missing.\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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
            "Return the same JSON plan contract as the standard planner.\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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
            "Return the same valid JSON plan contract. This is one bounded correction, not a retry.\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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
            "(string array), and confidence (number). Do not edit files.\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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
            "tests (string array), and gaps (string array).\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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
            "This is one bounded correction, not an unchanged retry.\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
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


_DEVELOPMENT_STATUSES = {"completed", "needs_human", "failed"}


def valid_development(value: dict[str, Any] | None) -> bool:
    if not value or value.get("status") not in _DEVELOPMENT_STATUSES:
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


def normalized_development(value: dict[str, Any] | None) -> str:
    """valid_development() only checks shape; the loop also needs to distinguish a
    genuinely malformed artifact (INVALID) from one of the three contractually-allowed
    statuses (see developer_task()'s prompt) — a "needs_human"/"failed" development
    still has real code changes worth sending to G2, the reviewer built to catch
    exactly the gaps a developer would self-report, rather than skipping it straight
    to a human stop."""
    if not valid_development(value):
        return "INVALID"
    return str((value or {}).get("status")).upper()


def summarize_invalid_artifact(label: str, artifact: dict[str, Any] | None) -> str:
    """Same motivation as summarize_gate_artifact(): "X did not produce a valid Y" on
    its own tells a human nothing they can act on. This is for the schema-validation
    failure case specifically (an artifact WAS returned and parsed as JSON, it just
    didn't pass valid_plan()/valid_development()/etc.) — showing the actual JSON the
    model produced lets a human see what's missing or malformed, instead of just being
    told "invalid" with no way to tell why short of reading the raw trace file."""
    if not artifact:
        return f"{label}: no structured output was returned."
    try:
        preview = json.dumps(artifact, ensure_ascii=False)[:600]
    except TypeError:
        preview = str(artifact)[:600]
    return f"{label}: structured output was returned but failed schema validation.\nRaw output:\n{preview}"


def summarize_gate_artifact(label: str, artifact: dict[str, Any] | None) -> str:
    """A human deciding whether to approve/reject a needs_human stop needs the gate's
    actual reasoning, not a fixed placeholder sentence — a generic "A red-team gate
    requires human review" tells them nothing they can act on without going and
    reading the raw JSON themselves (or asking someone else to). This renders the
    verdict, reason_code, top findings, and open questions directly into the text that
    ends up as the Telegram approval message's proposed_action."""
    if not artifact:
        return f"{label}: gate requires human review (no structured verdict available)."
    verdict = str(artifact.get("verdict") or "NEEDS_HUMAN")
    reason_code = artifact.get("reason_code")
    lines = [f"{label} verdict: {verdict}" + (f" ({reason_code})" if reason_code else "")]
    findings = artifact.get("findings")
    if isinstance(findings, list) and findings:
        shown = findings[:3]
        for item in shown:
            if not isinstance(item, dict):
                continue
            severity = item.get("severity", "?")
            # The schema's canonical key is "description" (see valid_finding()), but
            # models sometimes emit "summary" instead — which is exactly what makes
            # normalized_gate() correctly reject the artifact as INVALID and fall
            # into this same needs_human path in the first place. Fall back to
            # "summary" (and "message") so the human still sees real content instead
            # of a blank line when that's what happened, rather than silently hiding
            # the very reasoning that explains why the gate stopped.
            description = str(
                item.get("description")
                or item.get("summary")
                or item.get("message")
                or item.get("detail")
                or item.get("explanation")
                or ""
            )[:220]
            lines.append(f"- [{severity}] {description}")
        if len(findings) > len(shown):
            lines.append(f"...and {len(findings) - len(shown)} more finding(s)")
    unverified = artifact.get("unverified_items")
    if isinstance(unverified, list) and unverified:
        lines.append("Open questions:")
        for item in unverified[:3]:
            lines.append(f"- {str(item)[:220]}")
        if len(unverified) > 3:
            lines.append(f"...and {len(unverified) - 3} more open question(s)")
    return "\n".join(lines)


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


_STAGE_FAMILIES: dict[str, set[str]] = {
    "plan": {"plan", "plan-revision", "plan-escalation"},
    "g1": {"g1", "g1-recheck"},
    "develop": {"develop", "develop-revision"},
    "g2": {"g2", "g2-recheck"},
}


def _skip_worthy(family: str, artifact: dict[str, Any] | None) -> bool:
    """Whether a historical artifact for this stage family is settled enough that a
    resumed run() can reuse it outright instead of re-invoking the adapter. A gate
    (g1/g2) only counts once it actually PASSed — a REJECT or NEEDS_HUMAN verdict is
    exactly the kind of thing a resume should let re-run, now with any new human
    clarification in context."""
    if family == "plan":
        return valid_plan(artifact)
    if family in ("g1", "g2"):
        return normalized_gate(artifact) == "PASS"
    if family == "develop":
        return normalized_development(artifact) != "INVALID"
    return False


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


_FINDING_DESCRIPTION_ALIASES = ("description", "summary", "message", "detail", "explanation")
_FINDING_KNOWN_LENGTHS = {
    "severity": 100,
    "description": 2_000,
    "evidence_ref": 1_000,
    "requirement_ref": 1_000,
    "required_action": 2_000,
}
# A model producing REJECT/NEEDS_HUMAN findings with real substance but a slightly
# different key name (e.g. "summary" instead of "description", or extra fields like
# "id"/"file"/"evidence"/"impact") should still count as that verdict, not get
# reclassified as INVALID and silently skip the loop's own auto-revision retry (the
# "if gate == REJECT: run one bounded planner revision" branch in run()) — that
# skipping is exactly the failure the user hit 2026-08-13 (findings used
# id/severity/file/summary/evidence/impact; none of it was actually wrong, just
# differently named, and the whole gate got thrown out over it). Any string-valued
# field within a generous length cap is tolerated now, not just the five originally
# named ones — the previous-stage-gets-a-chance-to-fix-it behavior should trigger
# on genuinely malformed output, not on a model's harmless field-naming choice.
_FINDING_FALLBACK_LENGTH = 4_000


def valid_finding(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not any(
        isinstance(value.get(key), str) and value[key].strip()
        for key in _FINDING_DESCRIPTION_ALIASES
    ):
        return False
    for key, item in value.items():
        if isinstance(item, str):
            if len(item) > _FINDING_KNOWN_LENGTHS.get(key, _FINDING_FALLBACK_LENGTH):
                return False
        elif isinstance(item, bool):
            # Extra flag fields like "blocking": false — confirmed real, 2026-08-14 —
            # carry real signal and aren't the kind of malformed output this
            # validator exists to catch. Nested objects/arrays/null still aren't
            # accepted: those are open-ended enough to be worth staying strict about.
            continue
        elif isinstance(item, (int, float)):
            continue
        else:
            return False
    return True


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
