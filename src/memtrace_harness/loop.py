from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time
from typing import Any, TYPE_CHECKING

from memtrace_harness.adapters import ModelAdapter
from memtrace_harness.continuation import build_resume_envelope, with_resume_envelope
from memtrace_harness.fallback import (
    classify_execution_failure,
    error_signature,
    permits_cross_provider_fallback,
)
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.output_contracts import output_schema_path
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

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig


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
        verify_command: str | None = None,
        verify_timeout_seconds: int = 1200,
        config: "HarnessConfig | None" = None,
        agent_loop_enabled: bool = True,
    ) -> None:
        self.adapters = adapters
        self.fallback_adapters = fallback_adapters or {}
        self.role_profiles = role_profiles or {}
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client
        self.max_total_tokens = max_total_tokens
        self.approval_manager = approval_manager
        self.working_directory = working_directory
        self.verify_command = verify_command
        self.verify_timeout_seconds = verify_timeout_seconds
        self.config = config
        # Per-project capability flag (harness-scope.md's `agent_loop: disabled`, see
        # ProjectScope.agent_loop_enabled) — some projects only ever want operational
        # execution (run_operational_action), never real code changes. Both an explicit
        # instruction to Controller (controller_task() strips "run_planner" out of the
        # allowed-actions list it's told) and a deterministic backstop below (in case
        # Controller picks it anyway) enforce this, same fail-closed shape as every
        # other hard boundary in this file.
        self.agent_loop_enabled = agent_loop_enabled
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
            task=controller_task(
                task, stage="start", stages=stages, agent_loop_enabled=self.agent_loop_enabled
            ),
            trace_id=trace_id,
            stages=stages,
            stage="control-start",
            profile_id="controller",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, controller)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        controller_artifact = controller.artifact or {}
        start_actions = {"run_planner", "run_operational_action", "ask_human", "stop"}
        if not valid_controller(controller_artifact, start_actions):
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
        action = controller_artifact.get("action")
        if action == "run_planner" and not self.agent_loop_enabled:
            # Deterministic backstop for the instruction controller_task() already
            # gave it (agent_loop_enabled=False strips "run_planner" from the allowed
            # list in the prompt) — a misbehaving model picking it anyway must not
            # reach Planner/Developer on a project that opted out of development work.
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller chose run_planner, but this project has agent_loop "
                    "disabled (harness-scope.md) — real development work is not "
                    "available here. Use run_operational_action for execution-only "
                    "requests, or enable agent_loop for this project if development "
                    "work is actually wanted.",
                ),
                writeback=writeback,
            )
        if action == "run_operational_action":
            return self._persist(
                self._run_operational_action(task, trace_id, stages),
                writeback=writeback,
            )
        if action != "run_planner":
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
                artifact = plan.artifact or {}
                detail = (
                    summarize_plan_needs_human("Planner", artifact)
                    if artifact.get("status") == "needs_human" and _plan_structure_ok(artifact)
                    else summarize_invalid_artifact("Planner", artifact)
                )
                return self._persist(
                    self._summary(task, trace_id, stages, "needs_human", detail),
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
                    artifact = revised_plan.artifact or {}
                    detail = (
                        summarize_plan_needs_human(planner_name, artifact)
                        if artifact.get("status") == "needs_human" and _plan_structure_ok(artifact)
                        else summarize_invalid_artifact(planner_name, artifact)
                    )
                    return self._persist(
                        self._summary(task, trace_id, stages, "needs_human", detail),
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
            verification = self._run_deterministic_verification()
            if verification is not None and not verification["passed"]:
                g2 = self._deterministic_reject_result(
                    stages=stages,
                    trace_id=trace_id,
                    stage="g2",
                    profile_id="red-team",
                    verification=verification,
                )
            else:
                g2 = self._execute(
                    task=gate_task(
                        task, "G2", development.artifact or {}, plan.artifact or {},
                        verification=verification,
                    ),
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
                recheck_verification = self._run_deterministic_verification()
                if recheck_verification is not None and not recheck_verification["passed"]:
                    g2 = self._deterministic_reject_result(
                        stages=stages,
                        trace_id=trace_id,
                        stage="g2-recheck",
                        profile_id="red-team",
                        verification=recheck_verification,
                    )
                else:
                    g2 = self._execute(
                        task=gate_task(
                            task, "G2", development.artifact or {}, plan.artifact or {},
                            verification=recheck_verification,
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

    def _run_operational_action(
        self, task: TaskEnvelope, trace_id: str, stages: list[LoopStageResult]
    ) -> LoopSummary:
        """Controller chose run_operational_action: execute the request directly via
        Developer and report the result, skipping Planner/G1/G2/Converge entirely
        (2026-09-17, explicit user request) — for a request that isn't a code-development
        task (e.g. "check the current price and P&L on 2891/2884"), the plan-then-review
        ceremony doesn't fit and only adds latency/cost. This is a deliberately weaker
        guarantee than the full loop: nothing here reviews Developer's own report the way
        G2 does, so treat operational_action as suitable for read/execute-only requests,
        not code changes that need a second opinion."""
        result = self._execute(
            task=operational_task(task),
            trace_id=trace_id,
            stages=stages,
            stage="operate",
            profile_id="developer",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, result)
        if stopped:
            return stopped
        normalized = normalized_development(result.artifact)
        if normalized == "INVALID":
            return self._summary(
                task,
                trace_id,
                stages,
                "needs_human",
                summarize_invalid_artifact("Developer (operational action)", result.artifact),
            )
        status: LoopStatus = {
            "COMPLETED": "succeeded",
            "NEEDS_HUMAN": "needs_human",
            "FAILED": "failed",
        }[normalized]
        summary_text = str((result.artifact or {}).get("summary") or "(no summary given)")
        return self._summary(task, trace_id, stages, status, summary_text)

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
            if response.execution.status == "succeeded" and artifact is None:
                # The agent's answer may be entirely sound in substance and only
                # fail to come back as a bare JSON object (prose wrapper, markdown,
                # a slightly different shape) — that is a formatting slip, not a
                # reason to stop the loop and page a human. Ask a cheap, fixed
                # model to re-express the SAME content against this stage's
                # schema before falling back to invalid_output/needs_human. The
                # repaired artifact still goes through this stage's own
                # valid_*() check below, so a repair that can't actually recover
                # the intended fields is still caught, not silently trusted.
                repaired = self._repair_malformed_output(
                    profile_id=profile_id, raw_text=response.final_text
                )
                if repaired is not None:
                    artifact = repaired
                    execution = replace(
                        response.execution,
                        parse_warnings=[
                            *response.execution.parse_warnings,
                            "final_text was not valid JSON; repaired via chat-model reformatting",
                        ],
                    )
                    response = replace(response, execution=execution)
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

    def _repair_malformed_output(
        self, *, profile_id: str, raw_text: str
    ) -> dict[str, Any] | None:
        """Best-effort reformatting of a response that succeeded but didn't come
        back as a bare JSON object. Uses the same lightweight, fixed chat-provider
        already configured for cheap utility calls (see HarnessConfig.chat_provider/
        chat_model) — not the stage's own role provider, since the point is a plain
        reformatting pass, not a second opinion. Returns None (never raises) on any
        failure so the caller falls through to the existing invalid_output path."""
        if not self.config or not self.config.chat_provider or not raw_text.strip():
            return None
        try:
            schema_text = output_schema_path(profile_id).read_text(encoding="utf-8")
        except (ValueError, RuntimeError):
            return None
        prompt = (
            "An AI agent was supposed to reply with ONLY a single JSON object matching "
            "the JSON Schema below, but its response did not come back as a bare JSON "
            "object (extra prose, markdown fences, wrong shape, etc). Re-express the "
            "SAME information the agent already gave as a single JSON object that "
            "matches the schema. Do not invent facts or values the agent's response "
            "doesn't support — if a required field genuinely can't be determined from "
            "the response, use an honest empty/neutral value for it rather than "
            "fabricating one. Reply with ONLY the JSON object: no commentary, no "
            "markdown fences.\n\n"
            f"Schema:\n{schema_text}\n\nAgent's response:\n{raw_text[:20_000]}"
        )
        from memtrace_harness.cli_process import CliProcessRunner

        command = [self.config.command_for(self.config.chat_provider)]
        if self.config.chat_model:
            command.extend(["--model", self.config.chat_model])
        command.extend(["--print", prompt])
        try:
            result = CliProcessRunner().run(
                command,
                cwd=self.working_directory or Path.cwd(),
                timeout_seconds=30,
            )
        except Exception:
            return None
        if result.return_code != 0 or not result.stdout:
            return None
        return parse_json_object(result.stdout)

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

    def _run_deterministic_verification(self) -> dict[str, Any] | None:
        """Ground G2's review in a real build/test result instead of relying solely
        on the Developer's own self-reported evidence. A model claiming "I compiled
        this" (or a model that couldn't get tool permission to try) is not the same
        as it actually having happened — see the 2026-08-14 Beri stopSharing bug,
        which shipped with green unit tests but broken CloudKit runtime behavior.
        Returns None (no gating imposed) if this project has no verify_command
        configured; otherwise runs it directly via the harness, not the model, so its
        exit code is a hard precondition, not something an LLM can talk its way past."""
        if not self.verify_command or not self.working_directory:
            return None
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", self.verify_command],
                cwd=self.working_directory,
                capture_output=True,
                text=True,
                timeout=self.verify_timeout_seconds,
            )
            exit_code: int | None = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            timed_out = True
        return {
            "command": self.verify_command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "passed": exit_code == 0,
            "duration_seconds": round(time.monotonic() - started, 1),
            "stdout_tail": stdout[-4_000:],
            "stderr_tail": stderr[-4_000:],
        }

    def _deterministic_reject_result(
        self,
        *,
        stages: list[LoopStageResult],
        trace_id: str,
        stage: str,
        profile_id: str,
        verification: dict[str, Any],
    ) -> LoopStageResult:
        """A synthetic gate REJECT built from _run_deterministic_verification()'s
        output, standing in for an actual G2 model call. Reuses the existing
        REJECT->developer_revision_task machinery unchanged: this only needs to look
        like a normal gate artifact to the rest of run(), not literally be a model
        response."""
        detail = (verification.get("stderr_tail") or verification.get("stdout_tail") or "")[:1_500]
        reason = (
            "Verification command timed out"
            if verification.get("timed_out")
            else f"Verification command exited {verification.get('exit_code')}"
        )
        artifact = {
            "verdict": "REJECT",
            "reason_code": "test_gap",
            "findings": [
                {
                    "description": (
                        f"Deterministic verification failed before G2 review — "
                        f"{reason}. Command: {verification.get('command')}\n{detail}"
                    )
                }
            ],
            "unverified_items": [],
            "confidence": 1.0,
        }
        now = utc_now_iso()
        execution = CliExecution(
            provider="harness",
            status="succeeded",
            command=["/bin/sh", "-c", str(verification.get("command"))],
            exit_code=verification.get("exit_code"),
            started_at=now,
            completed_at=now,
            duration_ms=int((verification.get("duration_seconds") or 0) * 1000),
            role_profile_id=profile_id,
        )
        response = ModelResponse(
            adapter_id="harness-verify",
            role=profile_id,
            claims=[],
            execution=execution,
            requires_human_decision=False,
            final_text=json.dumps(artifact, ensure_ascii=False),
        )
        result = LoopStageResult(
            sequence=len(stages) + 1,
            stage=stage,
            profile_id=profile_id,
            state="succeeded",
            response=response,
            artifact=artifact,
        )
        stages.append(result)
        self.trace_store.save_stage_result(
            conversation_id=self._required_conversation_id(),
            run_id=trace_id,
            result=result,
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
            if summary.status == "budget_exhausted":
                reason = "budget_exhausted"
            elif _is_technical_output_failure(summary.recommendation):
                reason = "model_output_invalid"
            else:
                reason = "ambiguous_requirement"
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
                run_id=summary.conversation_id,
                task_id=summary.task.task_id,
                stage="loop_draft_write",
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
    task: TaskEnvelope,
    *,
    stage: str,
    stages: list[LoopStageResult],
    agent_loop_enabled: bool = True,
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
    if stage == "start":
        allowed = ["run_operational_action", "ask_human", "stop"]
        if agent_loop_enabled:
            allowed.insert(0, "run_planner")
    else:
        allowed = ["finish", "ask_human"]
    # run_planner starts the full plan-then-review development loop (Planner/G1/
    # Developer/G2) — pick it only for a request that actually changes this project's
    # code. run_operational_action skips straight to Developer and reports its result
    # directly, no plan or review in between — pick it for a request that just needs
    # something done/checked/executed once (fetch data, run an existing script,
    # inspect current state) and doesn't touch the codebase. Get this choice right:
    # picking run_planner for a check-only request wastes a full loop on nothing to
    # plan; picking run_operational_action for a real code change skips the review
    # that change would need.
    operational_action_notice = ""
    if stage == "start":
        operational_action_notice = (
            "run_planner starts the full plan-then-review development loop "
            "(Planner/G1/Developer/G2) — choose it only when the goal actually needs "
            "code changed in this project's repository. run_operational_action skips "
            "straight to Developer and reports its result directly, with no plan or "
            "review step — choose it when the goal just needs something done or "
            "checked once (run an existing script, fetch live data, inspect current "
            "state, report a number) without touching the codebase. Picking "
            "run_planner for a check-only request wastes a full review loop on "
            "nothing to plan; picking run_operational_action for a real code change "
            "skips the review that change would need — get this right.\n"
        )
        if not agent_loop_enabled:
            operational_action_notice += (
                "run_planner is not available for this project (agent_loop is "
                "disabled in its harness-scope.md) — use run_operational_action for "
                "anything executable, or ask_human/stop if the request genuinely "
                "requires a code change.\n"
            )
    # 2026-09-05: only the converge stage (after G2 PASS, right before the loop
    # reports "succeeded") may write to MemTrace — update_node/create_node are
    # explicitly disallowed for every other stage/role, so this is the one place
    # the KB reflects a task actually finishing, decided by the user.
    write_permission = (
        "\nThis is the converge stage (the loop finished, G2 already passed): you MAY "
        "also call MemTrace's update_node (or create_node if no existing node fits) to "
        "record that this task completed — e.g. updating the originating decision/task "
        "node's status, or leaving a completion note linked to it. Only do this if you "
        "already searched and found the actual node(s) to update; never invent an id or "
        "write to a node you have not just looked at. This is optional, not required — "
        "skip it if nothing in this run's context makes clear what to update.\n"
        if stage != "start"
        else ""
    )
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
            "Do not call any tool other than MemTrace's own lookup tools "
            "(search_nodes, get_node, list_nodes, traverse) — no file read, no directory "
            "listing, no git command, no web search, no browser, nothing else. This "
            "explicitly includes AGENTS.md, CLAUDE.md, an \"operating contract\", "
            "architecture docs, or any other convention you might normally check first: "
            "none of that exists in this sandbox (attempting to read it will only fail — "
            "permission denied, the sandbox is intentionally outside any granted "
            "directory — and waste the turn), and MemTrace search is how you'd look up "
            f"the KB equivalent instead.{write_permission}"
            "The task envelope's goal, the loop-snapshot context, and (when present) the "
            "project-scope and prior-discussion context items below are what you start "
            "from. The prior-discussion item is only a recent rolling window of this "
            "project's chat/decision history, not a full search — when that isn't enough "
            "(e.g. the goal references a specific past conversation/task/decision by name "
            "and it isn't in the window below, or references a KB node id), decide for "
            "yourself whether a MemTrace search would actually resolve it before asking a "
            "human — searching is available to you, not banned, but it costs a real call, "
            "so only reach for it when it would plausibly change your decision, not "
            "reflexively on every turn. If the goal text itself explicitly instructs you "
            "to look something up, you must do it, not just note that you could have. "
            "Whichever way you decide, `searched_history` and `reason` must say honestly "
            "whether you searched and, if so, what you found (or didn't) — never leave it "
            "ambiguous, and never claim you searched when you didn't. If any tool call you "
            "attempt is denied or errors, do not stop or explain what happened — "
            "immediately answer the JSON decision anyway using whatever you already have; "
            "a partial answer is always better than none.\n"
            f"{operational_action_notice}"
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
                # harness_resume_envelope: an actual same-conversation resume
                # checkpoint. "context": project scope + recent prior-discussion
                # (see TelegramGateway._project_context_items()) — added 2026-09-05
                # after Controller kept saying "I don't have context" for goals like
                # "接續 chat_3077a492 的任務" that only make sense with recent
                # discussion in view (chat_3077a492/chat_621c8bb9, generation 17).
                # Deliberately still just static text, no tool calls added — keeps
                # Controller's zero-tool-call sandbox guarantee intact, just gives it
                # more to read before deciding.
                if item.content_type in {"harness_resume_envelope", "context"}
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
    verification: dict[str, Any] | None = None,
) -> TaskEnvelope:
    items = [artifact_item(f"harness:{gate_name.lower()}-evidence", "Gate evidence", evidence)]
    if plan is not None:
        items.append(artifact_item("harness:accepted-plan", "Accepted plan", plan))
    verification_note = ""
    if verification is not None:
        items.append(
            artifact_item(
                "harness:deterministic-verification",
                "Harness-run build/test verification (not model-reported)",
                verification,
            )
        )
        verification_note = (
            "\nA deterministic build/test command was already run by the harness "
            "itself (see the 'deterministic-verification' context item) and passed "
            "before this review started — treat its exit code as ground truth over "
            "any conflicting claim in the evidence about whether it compiles/tests "
            "clean, but it does not substitute for reviewing scope, correctness, or "
            "quality."
        )
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
            f"{verification_note}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=items,
    )


def operational_task(task: TaskEnvelope) -> TaskEnvelope:
    """Controller chose run_operational_action: run this directly via Developer with
    no plan and no review afterward (see AgentLoopRunner._run_operational_action()).
    Reuses the same output contract as developer_task()'s (development.json) since
    changed_files/tests being empty is a perfectly valid answer for a request that
    never touched the codebase — no separate schema needed for this."""
    return stage_task(
        task,
        suffix="operate",
        goal=(
            f"Carry out this operational request directly — it is NOT a code-development "
            f"task, so there is no plan to follow and no review afterward: {task.goal}\n"
            "Use whatever tools/commands are actually appropriate (run a script, fetch "
            "data, inspect files or state) to really do this, rather than describing "
            "what should happen. If it requires information you don't have and can't "
            "discover yourself, say so in `summary` and use status 'needs_human' "
            "instead of guessing.\n"
            "Return only a valid JSON object after the work with status ('completed', "
            "'needs_human', or 'failed'), summary (string) containing the actual result "
            "you found or did, changed_files (string array, normally empty here), tests "
            "(string array, normally empty here), and gaps (string array).\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=task.context_items,
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


def _plan_structure_ok(value: dict[str, Any] | None) -> bool:
    """Structural checks shared by valid_plan() below, but without the
    status=='ready' gate — lets a caller tell "genuinely malformed JSON" apart
    from "well-formed JSON that honestly reports status='needs_human'". See
    valid_plan()'s note: conflating the two produced a real, reproduced bug
    (run_3fd1adaf7e8a, 2026-09-05) where a complete, valid Planner-escalation
    response with real open_questions was misreported as "structured output was
    returned but failed schema validation" — sounding like a parsing glitch a
    retry would fix, when the model had already done its job correctly and the
    human genuinely needed to answer something."""
    if not value:
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


def valid_plan(value: dict[str, Any] | None) -> bool:
    return bool(value) and value.get("status") == "ready" and _plan_structure_ok(value)


def summarize_plan_needs_human(label: str, value: dict[str, Any]) -> str:
    """A well-formed plan that honestly says status='needs_human' (structurally
    valid per _plan_structure_ok(), just not ready) — surfaces its own
    open_questions directly to the human, instead of being routed through
    summarize_invalid_artifact()'s "something is malformed" framing. See
    _plan_structure_ok()'s note for the bug this fixes."""
    questions = value.get("open_questions") or []
    lines = [f"{label}: plan needs human input before it's ready."]
    plan_text = value.get("plan")
    if isinstance(plan_text, str) and plan_text.strip():
        lines.append(plan_text.strip()[:2000])
    if questions:
        lines.append("Open questions:")
        lines.extend(f"- {q}" for q in questions[:10])
    return "\n".join(lines)


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


_TECHNICAL_OUTPUT_FAILURE_MARKERS = (
    "returned invalid structured output",
    "failed schema validation",
    "no structured output was returned",
)


def _is_technical_output_failure(recommendation: str) -> bool:
    """True for a needs_human stop caused by the MODEL's output not parsing/validating
    (see summarize_invalid_artifact() and the raw-text fallback above) — a technical
    glitch a retry can fix, never a genuine question with an answer a human could type.
    Routed to reason="model_output_invalid" instead of "ambiguous_requirement" so the
    Telegram message doesn't tell a human to "just answer" something that isn't a
    question — see the 2026-09-05 appr_4367790a7b70/appr_8daa7ac3707e confusion, where
    a Controller schema-validation failure was shown with "直接用文字回覆你的答案"
    and the human correctly couldn't make sense of it."""
    return any(marker in recommendation for marker in _TECHNICAL_OUTPUT_FAILURE_MARKERS)


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
