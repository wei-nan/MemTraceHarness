from __future__ import annotations

from dataclasses import replace
import json
import logging
import re
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, TYPE_CHECKING

from memtrace_harness import stage_skills
from memtrace_harness import final_gate
from memtrace_harness import verification as verify_result
from memtrace_harness.goal_plan import (
    MAX_CRITERIA,
    MAX_PHASES,
    GoalPlan,
    Phase,
    assignment_plan,
    default_goal_plan,
    judge_goal_check,
    parse_goal_plan,
    parse_phases,
    render_goal_report,
)
from memtrace_harness.cli_process import child_env
from memtrace_harness.adapters import ModelAdapter
from memtrace_harness.continuation import build_resume_envelope, with_resume_envelope
from memtrace_harness.completion_claims import KB_UPDATES_INSTRUCTION
from memtrace_harness.decision_card import CARD_INSTRUCTION, card_from_artifact, restrict_basis
from memtrace_harness.decision_records import PRECEDENT_CONTENT_TYPE, known_basis_ids
from memtrace_harness.fallback import (
    classify_execution_failure,
    error_signature,
    permits_cross_provider_fallback,
    permits_read_only_timeout_fallback,
)
from memtrace_harness.memtrace_client import MemTraceClient
from memtrace_harness.output_contracts import output_schema_path
from memtrace_harness.role_profiles import RoleProfile
from memtrace_harness.taiwantrade_mcp import mcp_server_spec, tool_catalog
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
from memtrace_harness.worktrees import IntegrationReport, RepoTaskWorkspace, TaskWorktree, WorktreeManager

if TYPE_CHECKING:
    from memtrace_harness.config import HarnessConfig

logger = logging.getLogger(__name__)


def _integration_has_changes(integration: IntegrationReport | list[dict[str, Any]] | None) -> bool:
    if isinstance(integration, list):
        return any(int(row.get("commits_ahead") or 0) > 0 for row in integration)
    return bool(integration and integration.has_changes)

class RunCancelled(Exception):
    def __init__(self, trace_id, stages):
        self.trace_id, self.stages = trace_id, stages


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
        loop_mode: str = "full",
        off_limits: list[str] | None = None,
        alert_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        worktree: TaskWorktree | None = None,
        worktree_manager: WorktreeManager | None = None,
        repo_root: Path | None = None,
        repo_workspaces: tuple[RepoTaskWorkspace, ...] = (),
        create_approvals: bool = True,
        edit_lock: tuple[Callable[[], bool], Callable[[], None]] | None = None,
        edit_lock_wait_seconds: float = 3600.0,
        edit_lock_poll_seconds: float = 5.0,
        model_override_profiles: frozenset[str] = frozenset(),
        remote_delivery: bool = False,
        delivery_handoff: Callable[[LoopSummary], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # (acquire, release) for a project whose tasks share one folder (no worktree): the
        # Developer stage waits for it, so two tasks never edit the same files, while every
        # other kind of task keeps running alongside. None when this task has the folder to
        # itself (its own worktree, or the project runs one task at a time).
        self.edit_lock = edit_lock
        # False for an unattended schedule run: its needs_human stop is only a notification.
        # An approval would let a later human reply resume the monitoring conversation with a
        # different goal (2026-10-05, chat_2e6f12ab), so none is opened.
        self.create_approvals = create_approvals
        # Profiles whose first candidate is a model the operator picked for this task alone: if it
        # fails for any reason the stage goes on to the profile's usual chain, not only on capacity errors.
        self.model_override_profiles = model_override_profiles
        self.edit_lock_wait_seconds = edit_lock_wait_seconds
        self.edit_lock_poll_seconds = edit_lock_poll_seconds
        self._sleep = sleep
        self._edit_lock_held = False
        # A task running in its own git worktree (see worktrees.py): `working_directory`
        # is then that worktree, `repo_root` the project's main checkout the finished
        # branch has to be merged back into. Controller's converge stage is told about
        # the pending merge and may choose "merge"; the harness performs it.
        self.worktree = worktree
        self.worktree_manager = worktree_manager
        self.repo_root = repo_root
        self.repo_workspaces = tuple(repo_workspaces)
        self.remote_delivery = remote_delivery
        self.delivery_handoff = delivery_handoff
        self.delivery: dict[str, Any] | None = None
        self.integration: IntegrationReport | list[dict[str, Any]] | None = None
        # Operator-facing out-of-band alert (the Harness wires this to the operator's chat) for
        # infrastructure faults the operator must fix — currently only a provider CLI
        # that cannot be launched at all, see _alert_cli_unavailable().
        self.alert_callback = alert_callback
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self.adapters = adapters
        self.fallback_adapters = fallback_adapters or {}
        self.role_profiles = role_profiles or {}
        controller_profile = self.role_profiles.get("controller")
        self.primary_agent = bool(controller_profile and controller_profile.context_policy == "primary-session")
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
        # "full": the plan/G1/develop/G2 loop the Controller assigns. "lean" (harness-scope.md `loop_mode: lean`):
        # the Harness adds no plan or review stage; a phase is develop, then the project's verify_command read
        # back to the Developer. Only a failure the task itself caused is sent back, once.
        self.loop_mode = "lean" if loop_mode == "lean" else "full"
        self._baseline_verification: dict[str, Any] | None = None
        self.off_limits = list(off_limits or [])
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
        try:
            return self._run(task, writeback=writeback, conversation_id=conversation_id)
        except RunCancelled as stopped:
            return self._persist(self._summary(
                task, stopped.trace_id, stopped.stages, "failed", "任務已取消，已停止後續步驟並保留現有工作。",
            ), writeback=writeback)
        finally:
            self._release_edit_lock()

    def _acquire_edit_lock(
        self, task: TaskEnvelope, trace_id: str, stages: list[LoopStageResult]
    ) -> LoopSummary | None:
        """Wait for the right to edit the shared folder. None once held (or when this task
        needs no lock); a needs_human summary if another development task never lets go."""
        if self.edit_lock is None or self._edit_lock_held:
            return None
        acquire, _ = self.edit_lock
        deadline = time.monotonic() + self.edit_lock_wait_seconds
        waited = False
        while not acquire():
            if time.monotonic() >= deadline:
                return self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Another development task has been editing this project's files for over "
                    f"{int(self.edit_lock_wait_seconds // 60)} minutes, so this one could not start "
                    "its development stage. It can be resumed once that task finishes.",
                )
            waited = True
            self._sleep(self.edit_lock_poll_seconds)
        self._edit_lock_held = True
        if waited:
            logger.info(f"{self._conversation_id}: got the project's edit lock after waiting")
        return None

    def _release_edit_lock(self) -> None:
        if self.edit_lock is not None and self._edit_lock_held:
            self._edit_lock_held = False
            try:
                self.edit_lock[1]()
            except Exception:
                logger.exception("releasing the project's edit lock failed")

    def _run(
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
        if not any(item.content_type == USER_REQUEST_CONTENT_TYPE for item in task.context_items):
            original = self.trace_store.conversation_user_request(self._conversation_id)
            if original:
                task = replace(task, context_items=[*task.context_items, user_request_item(original)])
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

        previous_goal_plan = self._previous_goal_plan(self._conversation_id)
        controller = self._execute(
            task=controller_task(
                task,
                stage="start",
                stages=stages,
                agent_loop_enabled=self.agent_loop_enabled,
                loop_mode=self.loop_mode,
                previous_goal_plan=previous_goal_plan,
                primary_agent=self.primary_agent,
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
        start_actions = {"run_planner", "run_operational_action", "ask_human", "stop", "no_action_needed"}
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
            # reach the plan or develop skill on a project that opted out of development work.
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
        if action == "run_planner" and self.remote_delivery and self.worktree is None:
            return self._persist(self._summary(task, trace_id, stages, "needs_human",
                "共享交付流程要求 Git worktree；請先授權有 commit 的 Git repo，不能在原目錄直接修改。"), writeback=writeback)
        if action == "run_operational_action":
            result = self._run_operational_action(task, trace_id, stages)
            if self.remote_delivery and _integration_has_changes(self._prepare_integration(task)):
                result = self._summary(task, trace_id, stages, "needs_human",
                    "這次操作產生程式變更，須改走開發、測試與 PR 交付流程；已保留 worktree，尚未推送或合併。")
            return self._persist(result, writeback=writeback)
        if action == "no_action_needed":
            # The goal is already met or needs nothing done (typically right after the operator answered
            # "the current state is acceptable"). This ends the task; routing it through needs_human
            # opened a fresh question every time and never finished (beri-android#140, four rounds).
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "succeeded",
                    "Controller 判斷不需要任何動作：" + str(controller_artifact.get("reason") or "(no reason given)"),
                ),
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

        goal_plan = parse_goal_plan(controller_artifact.get("goal_plan"))
        if self.primary_agent and goal_plan is None:
            return self._persist(self._summary(task, trace_id, stages, "needs_human",
                "主代理沒有提供可驗收的目標與步驟，任務尚未開始。"), writeback=writeback)
        goal_plan = goal_plan or default_goal_plan(task.goal)
        reconstructed, stale = self._reusable_for_plan(reconstructed, previous_goal_plan, goal_plan)
        if stale:
            stale_ids = {id(item) for item in stale}
            stages[:] = [item for item in stages if id(item) not in stale_ids]
        phases = list(goal_plan.phases)
        done = 0
        converge_round = 0
        while True:
            while done < len(phases):
                done += 1
                stopped = self._run_phase(
                    task=task,
                    trace_id=trace_id,
                    stages=stages,
                    writeback=writeback,
                    phase=phases[done - 1],
                    index=done,
                    total_phases=len(phases),
                    reconstructed=reconstructed,
                )
                if stopped:
                    return stopped
                if done == len(phases):
                    break
                # More phases are planned: the Controller looks at what this one produced against the
                # goal before the next one starts, and may drop, add or reorder what is left.
                progress = self._execute(
                    task=controller_task(
                        task,
                        stage="progress",
                        stages=stages,
                        goal_plan=GoalPlan(goal_plan.restated_goal, goal_plan.success_criteria, tuple(phases)),
                        phases_done=done,
                        primary_agent=self.primary_agent,
                    ),
                    trace_id=trace_id,
                    stages=stages,
                    stage=f"progress-{done}",
                    profile_id="controller",
                )
                stopped = self._stop_after_execution(task, trace_id, stages, progress)
                if stopped:
                    return self._persist(stopped, writeback=writeback)
                if not valid_controller(progress.artifact, {"next_phase", "revise_phases", "finish", "ask_human"}):
                    return self._persist(
                        self._summary(
                            task, trace_id, stages, "needs_human",
                            summarize_invalid_artifact("Controller (progress)", progress.artifact),
                        ),
                        writeback=writeback,
                    )
                progress_action = (progress.artifact or {}).get("action")
                if progress_action == "ask_human":
                    return self._persist(
                        self._summary(
                            task, trace_id, stages, "needs_human",
                            "Controller stopped between phases: "
                            + str((progress.artifact or {}).get("reason") or "(no reason given)"),
                        ),
                        writeback=writeback,
                    )
                if progress_action == "finish":
                    del phases[done:]  # the goal is already met; the converge check below confirms it
                elif progress_action == "revise_phases":
                    revised = parse_phases((progress.artifact or {}).get("phases"), limit=MAX_PHASES - done)
                    if revised is None:
                        return self._persist(
                            self._summary(
                                task, trace_id, stages, "needs_human",
                                "Controller asked to revise the remaining phases but gave none the harness "
                                "could use (each needs a title, a purpose and plan and/or develop; "
                                f"at most {MAX_PHASES} phases in all).",
                            ),
                            writeback=writeback,
                        )
                    phases[done:] = revised

            blocked = self._final_gate(task, trace_id, stages, phases, writeback=writeback)
            if blocked is not None:
                return blocked
            integration = self._prepare_integration(task)
            can_revise = len(phases) < MAX_PHASES
            converge_actions = {"finish", "ask_human"} | (
                {"merge"} if _integration_has_changes(integration) else set()
            ) | ({"revise_phases"} if can_revise else set())
            converge_round += 1
            current_plan = GoalPlan(goal_plan.restated_goal, goal_plan.success_criteria, tuple(phases))
            converge = self._execute(
                task=controller_task(
                    task,
                    stage="converge",
                    stages=stages,
                    integration=integration,
                    goal_plan=current_plan,
                    phases_done=done,
                    can_revise=can_revise,
                    primary_agent=self.primary_agent,
                ),
                trace_id=trace_id,
                stages=stages,
                stage="converge" if converge_round == 1 else f"converge-{converge_round}",
                profile_id="controller",
            )
            stopped = self._stop_after_execution(task, trace_id, stages, converge)
            if stopped:
                return self._persist(stopped, writeback=writeback)
            if not valid_controller(converge.artifact, converge_actions):
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
            converge_action = (converge.artifact or {}).get("action")
            if converge_action == "revise_phases":
                # The goal is not reached yet and the Controller says what still has to be done: run it,
                # then check again. Bounded by MAX_PHASES, so this cannot go on forever.
                more = parse_phases((converge.artifact or {}).get("phases"), limit=MAX_PHASES - len(phases))
                if more is None:
                    return self._persist(
                        self._summary(
                            task, trace_id, stages, "needs_human",
                            "Controller said the goal is not reached yet and asked for more phases, but gave "
                            "none the harness could use (or the phase limit is reached). "
                            + str((converge.artifact or {}).get("reason") or ""),
                        ),
                        writeback=writeback,
                    )
                phases.extend(more)
                continue
            break

        goal_check = (converge.artifact or {}).get("goal_check")
        verdict = judge_goal_check(current_plan, goal_check)
        report = render_goal_report(current_plan, goal_check, verdict)
        merge_note = ""
        if converge_action in {"finish", "merge"} and not verdict.complete:
            # The Controller wants to close the task but its own check says the user's goal is not fully
            # met (or it never checked). Report it as unfinished instead of calling half the job done.
            problems = [verdict.problem, *(f"沒做到：{item}" for item in verdict.unmet)]
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "這個任務只完成了一部分，原始目標還沒有達成，所以沒有標成完成。\n"
                    + "\n".join(item for item in problems if item)
                    + (f"\n\n{report}" if report else ""),
                ),
                writeback=writeback,
            )
        if self.remote_delivery and converge_action in {"finish", "merge"}:
            if integration is None:
                return self._persist(self._summary(task, trace_id, stages, "needs_human",
                    "無法確認 worktree 的整合狀態，停止交付並保留內容。"), writeback=writeback)
            if _integration_has_changes(integration):
                # Recheck the final files after all phases/gates. Baseline failures and unrun tests
                # are information in ordinary loops, but cannot authorize automatic delivery.
                configured = all(item.verify_command for item in self.repo_workspaces) if self.repo_workspaces else bool(self.verify_command)
                verification = self._verify() if configured else None
                passed = verification and verification.get("status") == "passed" and all(
                    row.get("status") == "passed" for row in verification.get("repos", []))
                if not passed:
                    return self._persist(self._summary(task, trace_id, stages, "needs_human",
                        "自動交付需要各 repo 的 verify_command 實際通過；目前缺少測試或仍有未通過的測試，保留 worktree。"), writeback=writeback)
                self.delivery = {"state": "pending", "integration": integration if isinstance(integration, list) else [integration.to_dict()]}
                return self._persist(self._summary(task, trace_id, stages, "delivering",
                    (report + "\n\n" if report else "") + "本機開發與驗證通過，接著推送 PR、確認 CI 與衝突，再合併遠端 main。"), writeback=writeback)
            # No changes means no branch to deliver and no remote merge to claim.
            return self._persist(self._summary(task, trace_id, stages, "succeeded",
                (report + "\n" if report else "") + "已檢查目標；沒有需要推送的程式變更。"), writeback=writeback)
        if converge_action == "merge" or (
            isinstance(integration, list) and converge_action == "finish"
            and _integration_has_changes(integration)
        ):
            self._report_progress("integrate", "running", None)
            merged = self._merge_worktree(task)
            self._report_progress("integrate", "succeeded" if merged.ok else "failed", {"delivery": self.delivery})
            if not merged.ok:
                return self._persist(
                    self._summary(task, trace_id, stages, "needs_human", merged.detail),
                    writeback=writeback,
                )
            merge_note = f" {merged.detail}"
        elif converge_action != "finish":
            return self._persist(
                self._summary(
                    task,
                    trace_id,
                    stages,
                    "needs_human",
                    "Controller chose not to finish: "
                    + str((converge.artifact or {}).get("reason") or "(no reason given)")
                    + (f"\n\n{report}" if report else ""),
                ),
                writeback=writeback,
            )
        elif _integration_has_changes(integration):
            if isinstance(integration, list):
                merge_note = " 變更仍留在各 repo 的獨立分支，尚未合併。"
            else:
                merge_note = (
                    f" The task's changes are on branch '{integration.branch}' in "
                    f"{self.working_directory} and have NOT been merged into "
                    f"'{integration.base_branch}'."
                )
        return self._persist(
            self._summary(
                task,
                trace_id,
                stages,
                "succeeded",
                (f"{report}\n\n" if report else "")
                + (
                    "The bounded local loop completed with draft evidence. This does not assert "
                    "a MemTrace server-side gate or task-state transition."
                    + merge_note
                ),
            ),
            writeback=writeback,
        )

    @staticmethod
    def _stage(index: int, name: str) -> str:
        """A stage's recorded name. Phase 1 keeps the plain names; later phases are prefixed
        (``p2-develop``) so a resumed run can tell whose artifact is whose."""
        return name if index == 1 else f"p{index}-{name}"

    def _run_phase(
        self,
        *,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        writeback: bool,
        phase: Phase,
        index: int,
        total_phases: int,
        reconstructed: dict[tuple[int, str], LoopStageResult],
    ) -> LoopSummary | None:
        """Run one phase of the goal plan: the skills the Controller chose for it, each followed by the
        independent review that skill always gets (``plan`` by G1, ``develop`` by the harness's own build/test
        and G2). None when the phase settled; otherwise the persisted summary that ends the whole run."""
        phase_items = phase_context_items(phase, index, total_phases, stages)
        g1_artifacts: list[dict[str, Any] | None] = []
        if "plan" in phase.skills and self.loop_mode != "lean":
            reused_plan = reconstructed.get((index, "plan"))
            if reused_plan is not None:
                plan = reused_plan
            else:
                plan = self._execute(
                    task=planner_task(task, phase_items=phase_items),
                    trace_id=trace_id,
                    stages=stages,
                    stage=self._stage(index, "plan"),
                    profile_id="plan",
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

            reused_g1 = reconstructed.get((index, "g1"))
            if reused_g1 is not None:
                g1 = reused_g1
                gate = "PASS"
            else:
                g1 = self._execute(
                    task=gate_task(task, "G1", plan.artifact or {}),
                    trace_id=trace_id,
                    stages=stages,
                    stage=self._stage(index, "g1"),
                    profile_id="review",
                )
                stopped = self._stop_after_execution(task, trace_id, stages, g1)
                if stopped:
                    return self._persist(stopped, writeback=writeback)

                g1_artifacts.append(g1.artifact)
                gate = normalized_gate(g1.artifact)
                reason_code = str((g1.artifact or {}).get("reason_code", "other"))
                if gate == "REJECT" and reason_code in G1_MISSCOPED_CODES:
                    # The gate judged the plan by implementation standards. Revising the plan
                    # cannot satisfy "the code doesn't exist yet", so ask the gate again with
                    # the correction instead of burning a planner revision and a second gate.
                    g1 = self._execute(
                        task=gate_task(task, "G1", plan.artifact or {}, rescope=True),
                        trace_id=trace_id,
                        stages=stages,
                        stage=self._stage(index, "g1-rescope"),
                        profile_id="review",
                    )
                    stopped = self._stop_after_execution(task, trace_id, stages, g1)
                    if stopped:
                        return self._persist(stopped, writeback=writeback)
                    gate = normalized_gate(g1.artifact)
                    reason_code = str((g1.artifact or {}).get("reason_code", "other"))
                    if gate == "REJECT" and reason_code in G1_MISSCOPED_CODES:
                        return self._persist(
                            self._summary(
                                task,
                                trace_id,
                                stages,
                                "needs_human",
                                "G1 (plan gate) rejected the plan twice for "
                                f"'{reason_code}', which cannot apply before any code exists — the "
                                "gate is judging the plan as if it were an implementation, so the plan "
                                "was not sent back for revision. Its findings, for reference:\n"
                                + summarize_gate_artifact("G1", g1.artifact),
                            ),
                            writeback=writeback,
                        )
                if gate == "REJECT" and reason_code != "missing_input":
                    use_opus = reason_code == "reasoning_gap"
                    revised_plan = self._execute(
                        task=(
                            planner_escalation_task(task, plan.artifact or {}, g1.artifact or {}, phase_items=phase_items)
                            if use_opus
                            else planner_revision_task(task, plan.artifact or {}, g1.artifact or {}, phase_items=phase_items)
                        ),
                        trace_id=trace_id,
                        stages=stages,
                        stage=self._stage(index, "plan-escalation" if use_opus else "plan-revision"),
                        profile_id="plan-escalation" if use_opus else "plan",
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
                        task=gate_task(task, "G1", plan.artifact or {}, tighten=True),
                        trace_id=trace_id,
                        stages=stages,
                        stage=self._stage(index, "g1-recheck"),
                        profile_id="review",
                    )
                    stopped = self._stop_after_execution(task, trace_id, stages, g1)
                    if stopped:
                        return self._persist(stopped, writeback=writeback)
                    g1_artifacts.append(g1.artifact)
                    gate = normalized_gate(g1.artifact)

                if gate != "PASS":
                    detail = summarize_gate_artifact("G1", g1.artifact)
                    return self._persist(
                        self._summary(task, trace_id, stages, "needs_human", detail),
                        writeback=writeback,
                    )
            plan_artifact = plan.artifact or {}
        else:
            # No plan skill: the Controller assigned this work directly, so there is no plan to gate.
            plan_artifact = assignment_plan(phase)
        if "develop" not in phase.skills:
            return None

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

        blocked = self._acquire_edit_lock(task, trace_id, stages)
        if blocked is not None:
            return self._persist(blocked, writeback=writeback)

        reused_develop = reconstructed.get((index, "develop"))
        if reused_develop is not None:
            development = reused_develop
        else:
            development = self._execute(
                task=developer_task(
                    task, plan_artifact, deferred_findings(*g1_artifacts) or None, phase_items=phase_items
                ),
                trace_id=trace_id,
                stages=stages,
                stage=self._stage(index, "develop"),
                profile_id="develop",
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
        if self.loop_mode == "lean":
            return self._lean_verify(
                task=task, trace_id=trace_id, stages=stages, writeback=writeback, index=index,
                plan_artifact=plan_artifact, development=development,
            )
        # A developer-reported "needs_human"/"failed" status is not itself a reason to
        # stop and page a human — G2 is the reviewer built to catch exactly the gaps a
        # developer would flag (missing compile evidence, self-expanded scope, etc.)
        # and, via its own REJECT verdict, drive the same one-bounded-revision loop it
        # already runs for a "completed" development. Only G2's own verdict (or the
        # INVALID case above) should end this without a human.

        reused_g2 = reconstructed.get((index, "g2"))
        if reused_g2 is not None:
            g2 = reused_g2
            g2_verdict = "PASS"
        else:
            verification = self._verify()
            if verification is not None and verification["status"] in verify_result.BLOCKING:
                g2 = self._deterministic_reject_result(
                    stages=stages,
                    trace_id=trace_id,
                    stage=self._stage(index, "g2"),
                    profile_id="review",
                    verification=verification,
                )
            else:
                g2 = self._execute(
                    task=gate_task(
                        task, "G2", development.artifact or {}, plan_artifact,
                        verification=verification,
                        deferred=deferred_findings(*g1_artifacts) or None,
                    ),
                    trace_id=trace_id,
                    stages=stages,
                    stage=self._stage(index, "g2"),
                    profile_id="review",
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
                        plan_artifact,
                        development.artifact or {},
                        g2.artifact or {},
                    ),
                    trace_id=trace_id,
                    stages=stages,
                    stage=self._stage(index, "develop-revision"),
                    profile_id="develop",
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
                recheck_verification = self._verify()
                if recheck_verification is not None and recheck_verification["status"] in verify_result.BLOCKING:
                    g2 = self._deterministic_reject_result(
                        stages=stages,
                        trace_id=trace_id,
                        stage=self._stage(index, "g2-recheck"),
                        profile_id="review",
                        verification=recheck_verification,
                    )
                else:
                    g2 = self._execute(
                        task=gate_task(
                            task, "G2", development.artifact or {}, plan_artifact,
                            verification=recheck_verification,
                        ),
                        trace_id=trace_id,
                        stages=stages,
                        stage=self._stage(index, "g2-recheck"),
                        profile_id="review",
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
        return None

    def _prepare_integration(self, task: TaskEnvelope) -> IntegrationReport | list[dict[str, Any]] | None:
        """Commit what Developer left in the task's worktree and work out what merging
        it back would involve. None when this task is not running in a worktree."""
        if len(self.repo_workspaces) > 1:
            if not self.worktree_manager:
                return None
            reports: list[dict[str, Any]] = []
            try:
                for item in self.repo_workspaces:
                    if not item.worktree:
                        return None
                    self.worktree_manager.commit_all(
                        item.worktree, f"harness: {task.goal.strip().splitlines()[0][:72]}"
                    )
                    report = self.worktree_manager.integration_report(item.repo_root, item.worktree)
                    reports.append({"repo": item.name, **report.to_dict()})
            except Exception:
                logger.exception("could not prepare multi-repo integration reports")
                return None
            self.integration = reports
            if _integration_has_changes(reports):
                self.delivery = {"state": "unmerged"}
            return reports
        if not (self.worktree and self.worktree_manager and self.repo_root):
            return None
        try:
            self.worktree_manager.commit_all(
                self.worktree, f"harness: {task.goal.strip().splitlines()[0][:72]}"
            )
            self.integration = self.worktree_manager.integration_report(
                self.repo_root, self.worktree
            )
            if self.integration.has_changes:
                self.delivery = {"state": "unmerged"}
        except Exception:
            logger.exception("could not prepare the worktree integration report")
            return None
        return self.integration

    def _merge_worktree(self, task: TaskEnvelope):
        from memtrace_harness.worktrees import MergeResult

        if len(self.repo_workspaces) > 1:
            if not self.worktree_manager:
                self.delivery = {"state": "blocked"}
                return MergeResult(False, "Multi-repo merge has no worktree manager.", [])
            reports = self.integration if isinstance(self.integration, list) else []
            blocked = [r for r in reports if r.get("commits_ahead", 0) and (
                not r.get("main_checkout_clean") or r.get("merges_cleanly") is not True
            )]
            if blocked:
                self.delivery = {"state": "blocked"}
                names = ", ".join(str(row.get("repo")) for row in blocked)
                return MergeResult(False, f"尚未合併任何 repo；整合前檢查未通過：{names}。", [])
            merged: list[str] = []
            for item in self.repo_workspaces:
                if not item.worktree:
                    continue
                report = next((r for r in reports if r.get("repo") == item.name), {})
                if not report.get("commits_ahead", 0):
                    continue
                result = self.worktree_manager.merge(
                    item.repo_root, item.worktree,
                    f"Merge {item.worktree.branch}: {task.goal.strip().splitlines()[0][:72]}",
                )
                if not result.ok:
                    self.delivery = {
                        "state": "partial" if merged else "blocked", "merged_repos": list(merged),
                        "pending_repos": [r["repo"] for r in reports if r.get("commits_ahead", 0) and r["repo"] not in merged],
                    }
                    detail = result.detail + (" Already merged: " + ", ".join(merged) + "." if merged else "")
                    return MergeResult(False, detail, result.conflicts)
                merged.append(item.name)
            self.delivery = {"state": "merged", "repos": merged}
            return MergeResult(True, "已分別合併 repo：" + (", ".join(merged) if merged else "沒有待合併變更") + "。", [])

        if not (self.worktree and self.worktree_manager and self.repo_root):
            self.delivery = {"state": "blocked"}
            return MergeResult(False, "Controller chose merge, but this task has no worktree.", [])
        result = self.worktree_manager.merge(
            self.repo_root,
            self.worktree,
            f"Merge {self.worktree.branch}: {task.goal.strip().splitlines()[0][:72]}",
        )
        self.delivery = {"state": "merged" if result.ok else "blocked", "target_branch": self.worktree.base_branch}
        if result.ok or not result.conflicts:
            return result
        return MergeResult(
            False,
            result.detail + " Conflicting files: " + ", ".join(result.conflicts[:20])
            + f". The work stays on branch '{self.worktree.branch}'.",
            result.conflicts,
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
            profile_id="develop",
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
        self._check_cancelled(trace_id, stages)
        intent = None
        for item in task.context_items:
            if item.ref == "harness:phase" and item.body:
                try:
                    phase = json.loads(item.body)
                except (TypeError, json.JSONDecodeError):
                    continue
                if isinstance(phase, dict):
                    intent = {"phase": phase}
        self._report_progress(stage, "running", intent)
        candidates = [
            self.adapters[profile_id],
            *self.fallback_adapters.get(profile_id, []),
        ]
        fallback_from_model: str | None = None
        effective_task = task
        last_result: LoopStageResult | None = None

        for attempt_index, adapter in enumerate(candidates):
            quota_bucket = _adapter_quota_bucket(adapter)
            if not getattr(adapter, "execution_source_id", None) and not self.trace_store.quota_bucket_available(quota_bucket):
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
            self._report_progress(stage, state, artifact)
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
            if not getattr(adapter, "execution_source_id", None):
                self.trace_store.record_provider_availability(
                    provider=response.execution.provider,
                    model=response.execution.resolved_model or response.execution.requested_model,
                    quota_bucket=quota_bucket,
                    failure_category=response.execution.failure_category,
                    error_signature=error_signature(response.execution),
                    cooldown_seconds=self._fallback_cooldown_seconds(profile_id),
                )
            self._active_checkpoint_id = envelope.checkpoint_id
            last_result = result
            self._check_cancelled(trace_id, stages)

            if response.execution.status == "succeeded":
                return result
            if response.execution.failure_category in {"cli_unavailable", "authentication"}:
                self._alert_cli_unavailable(
                    profile_id=profile_id,
                    adapter=adapter,
                    execution=response.execution,
                    next_adapter=(
                        candidates[attempt_index + 1]
                        if attempt_index + 1 < len(candidates)
                        else None
                    ),
                )
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

        selected = self.config.chat_candidates_for()
        if not selected:
            return None
        command = self.config.chat_command(*selected[0], prompt)
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

    def _previous_goal_plan(self, conversation_id: str) -> GoalPlan | None:
        """The goal plan the Controller wrote the last time this conversation started, if it wrote a usable one."""
        latest: dict[str, Any] | None = None
        for row in self.trace_store.get_conversation_pipeline(conversation_id):
            if row.get("stage") == "control-start" and row.get("state") == "succeeded":
                detail = self.trace_store.get_turn_detail(row["turn_id"])
                latest = detail.get("artifact") if detail else None
        return parse_goal_plan((latest or {}).get("goal_plan"))

    @staticmethod
    def _reusable_for_plan(
        reconstructed: dict[tuple[int, str], LoopStageResult],
        previous: GoalPlan | None,
        current: GoalPlan,
    ) -> tuple[dict[tuple[int, str], LoopStageResult], list[LoopStageResult]]:
        """Which settled stages of earlier runs still belong to the plan the Controller just wrote. Stages are
        recorded by phase number, so a re-planned goal must not inherit another phase's artifact: a phase is
        reused only while it and every phase before it are unchanged (same title and skills). With no earlier
        plan on record, only a default one-phase plan may reuse the stages of the older single-pass loop."""
        def same_prefix(count: int) -> bool:
            if previous is None:
                return current.defaulted and count <= 1
            if count > len(previous.phases) or count > len(current.phases):
                return False
            return all(
                (a.title, a.skills) == (b.title, b.skills)
                for a, b in zip(previous.phases[:count], current.phases[:count])
            )

        kept = {key: value for key, value in reconstructed.items() if same_prefix(key[0])}
        stale = [value for key, value in reconstructed.items() if key not in kept]
        return kept, stale

    def _reconstruct_progress(self, conversation_id: str) -> dict[tuple[int, str], LoopStageResult]:
        """Scan this conversation's full turn history (across every prior run() call,
        not just this one) for the latest skip-worthy artifact per stage family — see
        _skip_worthy(). Returns a synthetic, unpersisted LoopStageResult per family
        (state="reused") built from trace_store data alone, without invoking any
        adapter; run() uses these to bypass re-executing stages that already reached a
        genuinely reusable outcome."""
        found: dict[tuple[int, str], LoopStageResult] = {}
        for row in self.trace_store.get_conversation_pipeline(conversation_id):
            if row.get("state") != "succeeded":
                continue
            phase_number, base_stage = split_stage_name(str(row["stage"]))
            family = next(
                (name for name, names in _STAGE_FAMILIES.items() if base_stage in names),
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
            found[(phase_number, family)] = LoopStageResult(
                sequence=int(row.get("sequence") or 0),
                stage=f"{row['stage']}-reused",
                profile_id=str(row.get("profile_id")),
                state="reused",
                response=response,
                artifact=artifact,
                attempt_index=int(row.get("attempt_index") or 0),
            )
        return found

    def _run_deterministic_verification(
        self, directory: Path | None = None, *, command: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any] | None:
        """Ground G2's review in a real build/test result instead of relying solely
        on the Developer's own self-reported evidence. A model claiming "I compiled
        this" (or a model that couldn't get tool permission to try) is not the same
        as it actually having happened — see the 2026-08-14 Beri stopSharing bug,
        which shipped with green unit tests but broken CloudKit runtime behavior.
        Returns None (no gating imposed) if this project has no verify_command
        configured; otherwise runs it directly via the harness, not the model, so its
        exit code is a hard precondition, not something an LLM can talk its way past."""
        directory = directory or self.working_directory
        command = self.verify_command if command is None else command
        if not command or not directory:
            return None
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=timeout_seconds or self.verify_timeout_seconds,
                env=child_env(),
            )
            exit_code: int | None = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            timed_out = True
        return {
            "command": command,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "passed": exit_code == 0,
            "duration_seconds": round(time.monotonic() - started, 1),
            "stdout_tail": stdout[-4_000:],
            "stderr_tail": stderr[-4_000:],
        }

    def _final_gate(
        self,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        phases: list[Phase],
        *,
        writeback: bool,
    ) -> LoopSummary | None:
        """The final gate (final_gate.py), run by code once the work is done: off-limits and outside-the-folder
        writes, files outside the paths the Controller declared, secrets in what was added. Returns the persisted
        stop, or None to go on."""
        if self.working_directory is not None or len(self.repo_workspaces) > 1:
            self._report_progress("final-gate", "running", None)
        if len(self.repo_workspaces) > 1:
            developing = [phase for phase in phases if "develop" in phase.skills]
            declared_by_repo: dict[str, list[str]] = {}
            any_paths = bool(developing and all(phase.paths for phase in developing))
            if any_paths:
                for phase in developing:
                    for declared_path in phase.paths:
                        if "::" in declared_path:
                            name, path = declared_path.split("::", 1)
                            declared_by_repo.setdefault(name, []).append(path)
                        else:
                            declared_by_repo.setdefault(self.repo_workspaces[0].name, []).append(declared_path)
            all_findings: list[str] = []
            any_changes = False
            any_blocked = False
            for item in self.repo_workspaces:
                reported = [
                    str(name) for result in stages
                    if "develop" in result.stage and isinstance(result.artifact, dict)
                    for name in result.artifact.get("changed_files") or []
                ]
                changes = (
                    final_gate.collect_git_changes(item.working_directory, item.worktree.base_commit)
                    if item.worktree and item.worktree.base_commit else None
                )
                if changes is None:
                    changes = final_gate.collect_reported_changes(item.working_directory, reported)
                if not changes.files:
                    continue
                any_changes = True
                result = final_gate.check(
                    changes, directory=item.working_directory, off_limits=list(item.off_limits),
                    declared_paths=declared_by_repo.get(item.name) if any_paths else None,
                )
                any_blocked = any_blocked or (result.blocked and not final_gate.waived(task.goal, result))
                all_findings.extend(f"{item.name}: {finding.render()}" for finding in result.findings)
            if not any_changes:
                self._report_progress("final-gate", "succeeded", {"status": "skipped"})
                return None
            blocked = any_blocked
            self._record_harness_stage(
                stages=stages, trace_id=trace_id, stage="final-gate", profile_id="review",
                verification={"command": "final-gate-multi-repo", "exit_code": 1 if blocked else 0, "duration_seconds": 0},
                artifact={"verdict": "REJECT" if blocked else "PASS", "findings": [{"description": x} for x in all_findings], "confidence": 1.0},
            )
            if blocked:
                return self._persist(self._summary(task, trace_id, stages, "needs_human", "多 repo final gate 擋下變更：\n" + "\n".join(all_findings)), writeback=writeback)
            return None
        directory = self.working_directory
        if directory is None:
            return None
        reported = [
            str(name)
            for result in stages
            if "develop" in result.stage and isinstance(result.artifact, dict)
            for name in result.artifact.get("changed_files") or []
        ]
        changes = None
        if self.worktree is not None and self.worktree.base_commit:
            changes = final_gate.collect_git_changes(Path(directory), self.worktree.base_commit)
        if changes is None:
            if not reported:
                self._report_progress("final-gate", "succeeded", {"status": "skipped"})
                return None
            changes = final_gate.collect_reported_changes(Path(directory), reported)
        developing = [phase for phase in phases if "develop" in phase.skills]
        declared = (
            [path for phase in developing for path in phase.paths]
            if developing and all(phase.paths for phase in developing) else None
        )
        result = final_gate.check(changes, directory=Path(directory), off_limits=self.off_limits, declared_paths=declared)
        if not changes.files:
            self._report_progress("final-gate", "succeeded", {"status": "skipped"})
            return None
        artifact = result.to_dict()
        self._record_harness_stage(
            stages=stages, trace_id=trace_id, stage="final-gate", profile_id="review",
            verification={"command": "final-gate", "exit_code": 1 if result.blocked else 0, "duration_seconds": 0},
            artifact=artifact,
        )
        if result.blocked and not final_gate.waived(task.goal, result):
            return self._persist(
                self._summary(task, trace_id, stages, "needs_human", final_gate.summary_text(result)),
                writeback=writeback,
            )
        return None

    def _verify(self, *, progress_stage: str = "verify", notify_completion: bool = True) -> dict[str, Any] | None:
        configured = (
            any(item.verify_command for item in self.repo_workspaces)
            if len(self.repo_workspaces) > 1 else bool(self.verify_command and self.working_directory)
        )
        if not configured:
            return None
        self._report_progress(progress_stage, "running", None)
        result = self._verify_result()
        if result is not None and notify_completion:
            self._report_progress(progress_stage, "succeeded", {
                "verification_status": result["status"],
                "repo_verifications": [{"repo": row["repo"], "status": row["status"]} for row in result.get("repos", [])],
            })
        return result

    def _verify_result(self) -> dict[str, Any] | None:
        """The verify_command result, classified (see verification.py). When it fails in a task worktree, the same
        command is run once on the main checkout, so a failure the base already has is not charged to the task."""
        if len(self.repo_workspaces) > 1:
            results: list[dict[str, Any]] = []
            for item in self.repo_workspaces:
                if not item.verify_command:
                    continue
                result = self._run_deterministic_verification(
                    item.working_directory, command=item.verify_command,
                    timeout_seconds=item.verify_timeout_seconds,
                )
                if result is None:
                    continue
                baseline = None
                if (not result["passed"] and not verify_result.could_not_run(result)
                        and item.worktree and item.repo_root.resolve() != item.working_directory.resolve()):
                    baseline = self._run_deterministic_verification(
                        item.repo_root, command=item.verify_command,
                        timeout_seconds=item.verify_timeout_seconds,
                    )
                results.append({"repo": item.name, **verify_result.classify(result, baseline)})
            if not results:
                return None
            blocking = [row for row in results if row.get("status") in verify_result.BLOCKING]
            descriptions = [f"{row['repo']}: {verify_result.describe(row)}" for row in results]
            return {
                "command": "verify each selected repo",
                "exit_code": 1 if blocking else 0,
                "passed": not blocking,
                "status": "regression" if blocking else "passed",
                "duration_seconds": sum(float(row.get("duration_seconds") or 0) for row in results),
                "stdout_tail": "\n".join(descriptions)[-4000:],
                "stderr_tail": "",
                "repos": results,
            }
        result = self._run_deterministic_verification()
        if result is None:
            return None
        baseline = None
        if (
            not result["passed"]
            and not verify_result.could_not_run(result)
            and self.repo_root is not None
            and self.working_directory is not None
            and Path(self.repo_root).resolve() != Path(self.working_directory).resolve()
        ):
            if self._baseline_verification is None:
                self._baseline_verification = self._run_deterministic_verification(Path(self.repo_root))
            baseline = self._baseline_verification
        return verify_result.classify(result, baseline)

    def _verification_stage(
        self, *, stages: list[LoopStageResult], trace_id: str, stage: str, verification: dict[str, Any]
    ) -> LoopStageResult:
        """Record a lean-mode verification as a stage: REJECT for a regression, otherwise PASS with the plain
        description as a finding, so the Controller's converge step and the operator can see it."""
        blocking = verification["status"] in verify_result.BLOCKING
        descriptions = (
            [f"{item['repo']}: {verify_result.describe(item)}" for item in verification["repos"]]
            if isinstance(verification.get("repos"), list)
            else [verify_result.describe(verification)]
        )
        artifact = {
            "verdict": "REJECT" if blocking else "PASS",
            "reason_code": "test_gap" if blocking else "none",
            "findings": [{"description": item} for item in descriptions],
            "unverified_items": [],
            "confidence": 1.0,
            "verification_status": verification["status"],
            "repo_verifications": [{"repo": row["repo"], "status": row["status"]} for row in verification.get("repos", [])],
        }
        return self._record_harness_stage(stages=stages, trace_id=trace_id, stage=stage, profile_id="review",
                                          verification=verification, artifact=artifact)

    def _lean_verify(
        self,
        *,
        task: TaskEnvelope,
        trace_id: str,
        stages: list[LoopStageResult],
        writeback: bool,
        index: int,
        plan_artifact: dict[str, Any],
        development: LoopStageResult,
    ) -> LoopSummary | None:
        """Lean mode, after develop: run the project's verify_command and tell the Developer what it found. No
        model reviews the change. A regression goes back to the Developer once; a second one stops the task for a
        person. A command that could not run, or a failure the base already has, is recorded and the work goes on."""
        verification = self._verify(progress_stage=self._stage(index, "verify"), notify_completion=False)
        if verification is None:
            return None
        result = self._verification_stage(
            stages=stages, trace_id=trace_id, stage=self._stage(index, "verify"), verification=verification
        )
        if verification["status"] not in verify_result.BLOCKING:
            return None
        development = self._execute(
            task=developer_revision_task(task, plan_artifact, development.artifact or {}, result.artifact or {}),
            trace_id=trace_id,
            stages=stages,
            stage=self._stage(index, "develop-revision"),
            profile_id="develop",
        )
        stopped = self._stop_after_execution(task, trace_id, stages, development)
        if stopped:
            return self._persist(stopped, writeback=writeback)
        recheck = self._verify(progress_stage=self._stage(index, "verify-recheck"), notify_completion=False)
        if recheck is None:
            return None
        again = self._verification_stage(
            stages=stages, trace_id=trace_id, stage=self._stage(index, "verify-recheck"), verification=recheck
        )
        if recheck["status"] in verify_result.BLOCKING:
            return self._persist(
                self._summary(task, trace_id, stages, "needs_human", summarize_gate_artifact("Verification", again.artifact)),
                writeback=writeback,
            )
        return None

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
        artifact = {
            "verdict": "REJECT",
            "reason_code": "test_gap",
            "findings": [
                {
                    "description": (
                        "Deterministic verification failed before G2 review — "
                        + verify_result.describe(verification)
                    )
                }
            ],
            "unverified_items": [],
            "confidence": 1.0,
        }
        return self._record_harness_stage(
            stages=stages, trace_id=trace_id, stage=stage, profile_id=profile_id,
            verification=verification, artifact=artifact,
        )

    def _record_harness_stage(
        self,
        *,
        stages: list[LoopStageResult],
        trace_id: str,
        stage: str,
        profile_id: str,
        verification: dict[str, Any],
        artifact: dict[str, Any],
    ) -> LoopStageResult:
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
        self._report_progress(stage, "succeeded", artifact)
        return result

    def _report_progress(self, stage: str, state: str, artifact: dict[str, Any] | None) -> None:
        if self.progress_callback is not None:
            try:
                self.progress_callback(stage, state, artifact)
            except Exception:
                logger.exception("could not deliver stage progress")

    def _check_cancelled(self, trace_id: str, stages: list[LoopStageResult]) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise RunCancelled(trace_id, stages)

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
        if profile_id in self.model_override_profiles and attempt_index == 0:
            return True
        profile = self.role_profiles.get(profile_id)
        # A read-only profile's timeout also bypasses fallback_on (see permits_read_only_timeout_fallback).
        timed_out_read_only = profile is not None and permits_read_only_timeout_fallback(
            category, profile.permission
        )
        # cli_unavailable and authentication bypass the profile's fallback_on: that list is
        # about capacity policy, whereas a CLI that cannot launch or whose login has expired
        # never evaluated the request at all.
        if (
            profile is not None
            and category not in {"cli_unavailable", "authentication", "source_busy", "source_unavailable"}
            and not timed_out_read_only
            and category not in profile.fallback_on
        ):
            return False
        if not timed_out_read_only and not permits_cross_provider_fallback(category):
            return False
        if self.max_total_tokens is not None and any(
            item.response.execution.usage.completeness == "unavailable"
            for item in stages
        ):
            return False
        return True

    def _alert_cli_unavailable(
        self,
        *,
        profile_id: str,
        adapter: ModelAdapter,
        execution: CliExecution,
        next_adapter: ModelAdapter | None,
    ) -> None:
        """Tell the operator a provider CLI could not even be launched. Best-effort:
        an alert failure must never change the loop's own outcome."""
        if self.alert_callback is None:
            return
        source = getattr(adapter, "execution_source_id", None)
        failed = f"{getattr(adapter, 'provider', 'unknown')}" + (f"@{source}" if source else "") + f"/{getattr(adapter, 'model', None) or 'provider-default'}"
        if next_adapter is not None:
            follow_up = (
                f"已自動改用備援 {getattr(next_adapter, 'provider', 'unknown')}/"
                f"{getattr(next_adapter, 'model', None) or 'provider-default'}。"
            )
        else:
            follow_up = "沒有可用的備援，這個階段會停止。"
        detail = (execution.error or "").strip().splitlines()
        if execution.failure_category == "authentication":
            headline = f"⚠️ {profile_id} 角色的登入已失效：{failed}。{follow_up}"
            advice = "請在終端機重新登入該 CLI（例如 claude 的 /login）。"
        else:
            headline = f"⚠️ {profile_id} 角色的 CLI 無法啟動：{failed}。{follow_up}"
            advice = "請檢查該 CLI 的安裝或路徑（例如 App 更新後執行檔位置改變）。"
        try:
            self.alert_callback(
                f"{headline}\n{advice}\n"
                f"錯誤：{detail[0] if detail else '(no error text)'}"
            )
        except Exception:
            logger.exception("failed to deliver cli_unavailable alert")

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
                detail += f"\nError: {error_detail}"
            return self._summary(task, trace_id, stages, "failed", detail)
        if result.artifact is None:
            # No parseable JSON — the best a human can act on here is what the model
            # actually said, not just "it was invalid" (this is exactly the shape of
            # the 2026-08-12 Claude plan-mode diversion bug: the model's real content
            # existed, just wasn't in a form the loop could parse as its verdict).
            raw_snippet = (result.response.final_text or "").strip()
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
            delivery=self.delivery,
        )

    def _persist(self, summary: LoopSummary, *, writeback: bool) -> LoopSummary:
        if summary.status == "delivering" and self.delivery_handoff:
            self.delivery_handoff(summary)
        self.trace_store.save_loop_summary(summary)
        if (
            summary.status in {"needs_human", "budget_exhausted"}
            and self.approval_manager
            and self.create_approvals
        ):
            if summary.status == "budget_exhausted":
                reason = "budget_exhausted"
            elif _is_technical_output_failure(summary.recommendation):
                reason = "model_output_invalid"
            elif not self.agent_loop_enabled and "agent_loop" in summary.recommendation:
                # Stopped because this project's agent_loop is disabled, not for lack of
                # information — see the "config_change_required" message in approval.py.
                reason = "config_change_required"
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
                # The stage that stopped the loop is the last one executed; its own
                # distilled question (if it wrote a usable one) replaces the raw detail
                # in what the human is shown.
                decision_card=restrict_basis(
                    card_from_artifact(summary.stages[-1].artifact if summary.stages else None),
                    known_basis_ids(
                        "\n".join(
                            item.body
                            for item in summary.task.context_items
                            if item.content_type == PRECEDENT_CONTENT_TYPE
                        )
                    ),
                ),
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
            "plan",
            "plan-escalation",
            "review",
            "develop",
        }
        missing = sorted(required.difference(self.adapters))
        if missing:
            raise ValueError(f"Agent loop is missing skill adapters: {', '.join(missing)}")


USER_REQUEST_CONTENT_TYPE = "user_request"
MAX_USER_REQUEST_CHARS = 4_000


def user_request_item(text: str) -> ContextItem:
    """The user's own message, verbatim. The chat model hands the loop a one-line goal; planning from that
    alone is how a request with several parts ended up half done."""
    return ContextItem(
        ref="harness:user-request",
        title="The user's own words (verbatim; the task goal may be a shorter summary of this)",
        body=text.strip()[:MAX_USER_REQUEST_CHARS],
        content_type=USER_REQUEST_CONTENT_TYPE,
        source="harness",
    )

_GOAL_PLAN_START = (
    "When you choose run_planner you must also return `goal_plan`, your plan for the user's WHOLE request. "
    "Read the request in full: the `user_request` context item, when present, holds their own words and the task "
    "goal above may be a shorter summary of it, so plan for everything they asked, not only what the summary "
    "mentions. `restated_goal` says what the whole request amounts to. `success_criteria` (1 to "
    f"{MAX_CRITERIA}) are observable conditions that together mean the WHOLE request is done, one for each "
    "distinct thing they asked for. `phases` (at most "
    f"{MAX_PHASES}) are the ordered steps that get there. Each phase lists the skills it needs: [\"plan\"], "
    "[\"develop\"] or [\"plan\", \"develop\"], with a title, a purpose and its own acceptance conditions. "
    "Each phase also lists `paths`: the files or folders (globs allowed, relative to the project folder) it is "
    "expected to change, as narrowly as you honestly can; the harness stops the task if a change lands outside "
    "every declared path. Use an empty list only when you cannot tell. "
    "Review is never listed: every plan is reviewed (G1) and every development is reviewed (G2) automatically. "
    "Default to ONE phase: [\"plan\", \"develop\"] for ordinary work, or [\"develop\"] alone for a small, clear "
    "change that needs no plan, and keep every part of the request in that one phase. Every extra phase costs a "
    "full review round and an extra check, so add one only when later work genuinely depends on what an earlier "
    "phase produces, a design must be settled and agreed before anything is built ([\"plan\"] alone), or one pass "
    "cannot hold it all. Never split work into phases just to organize it, and never make a phase out of a "
    "single file, a document, or two small edits to the same thing. "
    "Skills you can use:\n{catalog}\n"
    "`goal_check` and `phases` must be null at this stage.\n"
)
_GOAL_PLAN_KEPT = (
    "The loop snapshot's `previous_goal_plan` is the plan this conversation had before. Keep its phases "
    "(same titles and skills) unless new information changes what has to be done: phases that already settled "
    "are reused only while their titles and skills are unchanged.\n"
)
_GOAL_CHECK = (
    "\nBefore deciding, check the user's original request against what the stages actually produced. Return "
    "`goal_check`: `verdict` ('met', 'partly_met' or 'not_met'); `criteria`, one entry for EACH success "
    "criterion in the loop snapshot's goal_plan (its text, a status of 'met', 'unmet' or 'unverified', and the "
    "evidence from the stage results that supports it); and `remaining`, the work still undone. Never mark a "
    "criterion 'met' without evidence in the stage results; use 'unverified' when it could not be confirmed "
    "independently. A task is finished only when every part of the original request is done, not when one "
    "phase went well. `goal_plan` must be null at this stage.\n"
)
_GOAL_CHECK_PROGRESS = (
    "Choose next_phase if the remaining plan still fits, revise_phases (and give the phases still to do in "
    "`phases`) if what you found changes it, finish only if every criterion is already met, and ask_human only "
    "for a real decision the operator must make.\n"
)
_GOAL_CHECK_CONVERGE = (
    "Copy each original criterion exactly once. Choose finish or merge only if every criterion is 'met' with evidence: the harness refuses to close "
    "a task otherwise and reports it to the operator as only partly done. If something is unmet and plan or "
    "develop can still do it, choose revise_phases and give those phases in `phases`; if it needs the operator, "
    "choose ask_human.\n"
)


# This project's harness-scope.md says `loop_mode: lean`.
_LEAN_NOTICE = (
    "This project runs lean: the harness adds no plan or review stage. Give every phase `develop` only (no `plan`); "
    "the harness runs the project's verify_command after each develop and hands the Developer what it found. "
    "Put what must hold in each phase's acceptance list.\n"
)


def _goal_plan_notice(
    stage: str,
    agent_loop_enabled: bool,
    goal_plan: GoalPlan | None,
    previous_goal_plan: GoalPlan | None,
    loop_mode: str = "full",
) -> str:
    if stage == "start":
        if not agent_loop_enabled:
            return "\n`goal_plan`, `goal_check` and `phases` must be null at this stage.\n"
        notice = "\n" + _GOAL_PLAN_START.replace("{catalog}", stage_skills.catalog())
        if loop_mode == "lean":
            notice += _LEAN_NOTICE
        return notice + (_GOAL_PLAN_KEPT if previous_goal_plan is not None else "")
    derive = ""
    if goal_plan is None or not goal_plan.success_criteria:
        derive = (
            "This goal has no recorded success criteria, so derive them yourself from the original request "
            "(the `user_request` context item when present, else the goal) and check against those.\n"
        )
    tail = _GOAL_CHECK_PROGRESS if stage == "progress" else _GOAL_CHECK_CONVERGE
    return _GOAL_CHECK + derive + tail + "`phases` is null unless you choose revise_phases.\n"


def phase_context_items(
    phase: Phase, index: int, total: int, stages: list[LoopStageResult]
) -> list[ContextItem]:
    """What the plan and develop skills are told about the phase they work on. Nothing for the default
    single phase, so an ordinary one-pass run reads exactly as it always did."""
    if total == 1 and index == 1 and not phase.acceptance and phase.skills == ("plan", "develop"):
        return []
    body: dict[str, Any] = {"phase": index, "of": total, **phase.to_dict()}
    earlier = [
        {"stage": item.stage, "artifact": artifact_snapshot(item.artifact)}
        for item in stages
        if item.profile_id in {"plan", "plan-escalation", "develop"} and item.state != "failed"
    ]
    if index > 1 and earlier:
        body["earlier_stage_results"] = earlier
    return [artifact_item("harness:phase", f"Phase {index} of {total}: {phase.title}", body)]


def controller_task(
    task: TaskEnvelope,
    *,
    stage: str,
    stages: list[LoopStageResult],
    agent_loop_enabled: bool = True,
    loop_mode: str = "full",
    integration: IntegrationReport | list[dict[str, Any]] | None = None,
    goal_plan: GoalPlan | None = None,
    previous_goal_plan: GoalPlan | None = None,
    phases_done: int = 0,
    can_revise: bool = False,
    primary_agent: bool = False,
) -> TaskEnvelope:
    snapshot: dict[str, Any] = {
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
    if goal_plan is not None:
        snapshot["goal_plan"] = goal_plan.to_dict()
        snapshot["phases_done"] = phases_done
    if previous_goal_plan is not None:
        snapshot["previous_goal_plan"] = previous_goal_plan.to_dict()
    if stage == "start":
        allowed = ["run_operational_action", "ask_human", "stop", "no_action_needed"]
        if agent_loop_enabled:
            allowed.insert(0, "run_planner")
    elif stage == "progress":
        allowed = ["next_phase", "revise_phases", "finish", "ask_human"]
    else:
        allowed = ["finish", "ask_human"]
        if _integration_has_changes(integration):
            allowed.insert(1, "merge")
        if can_revise:
            allowed.append("revise_phases")
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
            "(plan, develop and their reviews, in phases you plan) — choose it only when the goal actually needs "
            "code changed in this project's repository. run_operational_action skips "
            "straight to Developer and reports its result directly, with no plan or "
            "review step — choose it when the goal just needs something done or "
            "checked once (run an existing script, fetch live data, inspect current "
            "state, report a number) without touching the codebase. Picking "
            "run_planner for a check-only request wastes a full review loop on "
            "nothing to plan; picking run_operational_action for a real code change "
            "skips the review that change would need — get this right.\n"
            "no_action_needed ends the task as finished with nothing to do — choose it when "
            "the goal is already met or the operator has said the current state is acceptable "
            "and nothing should change. Do not use ask_human or stop for that: both open a new "
            "question for the operator, so the same question would come back after they "
            "answered. stop is for when a person has to act before this can go on.\n"
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
    # A resumed task carries the operator's earlier answer as "Human clarification:" (service.py). Asking
    # the same question again after that is the failure seen on 2026-10-14 (the operator picked "I will
    # switch the schedule myself", and the next round asked whether it had been switched, twice).
    answered_notice = ""
    if "Human clarification:" in task.goal:
        answered_notice = (
            "The goal ends with a \"Human clarification:\" — the operator already answered the question that "
            "stopped the earlier round. Treat that answer as settled and do NOT ask the same or an equivalent "
            "question again. If the answer gives the operator a step to do themselves, do everything that remains "
            "on your side and finish, stating that step as still pending on them in `reason`; do not "
            "ask_human or stop to wait for it. Ask again only about something the answer did not cover.\n"
        )
    integration_notice = ""
    if _integration_has_changes(integration):
        snapshot["integration"] = (
            integration if isinstance(integration, list) else integration.to_dict()
        )
        integration_notice = (
            "This task ran in its own git worktree, so its changes are NOT in the project's "
            "main checkout yet: they sit on the branch described by `integration` in the "
            "loop snapshot, and something has to merge them. Choose \"merge\" and the "
            "harness will merge that branch into the base branch for you (it refuses and "
            "reports back if the main checkout has uncommitted changes, or if the merge "
            "conflicts). Choose \"finish\" only if the work should deliberately stay "
            "unmerged on its branch; choose \"ask_human\" if `merges_cleanly` is false or "
            "you doubt the change should land. Say in `reason` why.\n"
        )
        if isinstance(integration, list):
            integration_notice = (
                "This task has isolated worktrees for several repos. Every repo has been verified and preflighted. "
                "Once the goal check is complete, the harness merges each changed repo separately; the Controller "
                "does not need to choose a repo or request parallel editing. The harness checks all repos before the first merge. "
                "If a later Git operation fails after an earlier repo merged, it reports exactly which repos landed "
                "and leaves the remaining branches intact.\n"
            )
    # Opt-in read-only market-data proxy (see taiwantrade_mcp.py); without this sentence the
    # "no other tool" rule below would stop Controller from ever using it.
    taiwantrade_tools = (
        " and the read-only `taiwantrade` market-data tools (quotes, daily data, screens, "
        "positions, balance) when the goal needs live TaiwanTrade data — it cannot place or "
        "change orders"
        if mcp_server_spec()
        else ""
    )
    # The Controller maintains the knowledge base but never calls a write tool: at the converge
    # stage it proposes `kb_updates`, which the harness validates and applies (see
    # completion_claims.py). The task's own node id, if there is one, is in the goal/context.
    write_permission = (
        f"\nThis is the converge stage (the loop finished, G2 already passed). {KB_UPDATES_INSTRUCTION}\n"
        "A human looking at the result in a browser or on a device is NOT a condition for finishing: "
        "the harness publishes the result and tells the operator, and the operator reviews it whenever "
        "they like while the harness moves on. Choose \"finish\"/\"merge\" when the developer and G2 "
        "outcomes support it, and list anything not independently confirmed in `reason`. Choose "
        "\"ask_human\" only for a real decision the operator must make (a requirement that is "
        "ambiguous, a safety or scope question, a merge that conflicts) — never merely to have "
        "someone eyeball the result.\n"
        if stage == "converge"
        else "\nkb_updates must be null at this stage.\n"
    )
    goal_notice = _goal_plan_notice(stage, agent_loop_enabled, goal_plan, previous_goal_plan, loop_mode)
    repo_scope_notice = ""
    repo_context = next((item for item in task.context_items if item.ref == "harness:repo-workspaces"), None)
    if repo_context and repo_context.body:
        try:
            selected_repos = json.loads(repo_context.body)
        except (TypeError, json.JSONDecodeError):
            selected_repos = []
        if isinstance(selected_repos, list) and len(selected_repos) > 1:
            names = [str(item.get("repo")) for item in selected_repos if isinstance(item, dict)]
            repo_scope_notice = (
                "This task may edit only these selected repo worktrees: " + ", ".join(names) + ". "
                "For every plan phase, qualify each `paths` entry as `<repo>::<relative path>` "
                "(for example `android::app/src/Main.kt`) so the final gate applies the right scope to each repo. "
                "Do not plan writes in any unselected repo.\n"
            )
    context_policy_notice = (
        "Continue as the project's single primary agent, using your current project conversation "
        "and this execution snapshot. This is a structured decision turn of the same conversation. "
        "Use the original request, settled human decisions and the latest clarification together. "
        "Skills are execution capabilities you invoke, not a handoff to another decision maker. "
        "Keep this decision read-only; use MemTrace lookup tools for missing knowledge. "
        "Return the structured decision for this boundary; do not emit chat action markers.\n"
        if primary_agent else (
            "You are deliberately running in a neutral, empty sandbox directory, not the "
            "project's actual repository — this is intentional so this stage stays cheap "
            "and fast, not a malfunction. Any working_directory/path mentioned in the goal "
            "text above is informational context for later stages (the plan and develop skills, "
            "which do have real repo access), not something you can or should verify yourself.\n"
            "Do not call any tool other than MemTrace's own lookup tools "
            f"(search_nodes, get_node, list_nodes, traverse){taiwantrade_tools} — no file read, "
            "directory listing, no git command, no web search or browser. This includes AGENTS.md, CLAUDE.md, "
            "operating contracts and architecture docs: none exists in this sandbox. "
            "Use the supplied snapshot and MemTrace evidence.\n"
        )
    )
    return stage_task(
        task,
        suffix=f"controller-{stage}",
        goal=(
            f"Act as the bounded loop controller for {stage}. Original goal: {task.goal}\n"
            f"{context_policy_notice}{write_permission}"
            "The task envelope's goal, the loop-snapshot context, and (when present) the "
            "project-scope, knowledge-base-locations, and prior-discussion context items "
            "below are what you start from. The prior-discussion item is only a recent "
            "rolling window of this project's chat/decision history, not a full search; "
            "older history lives in the cold-memory workspace named by the "
            "knowledge-base-locations item — when that isn't enough "
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
            f"{answered_notice}"
            f"{goal_notice}"
            f"{integration_notice}"
            f"{repo_scope_notice}"
            f"{_PRECEDENT_NOTICE}"
            "The JSON schema enforced on this call's output is shared by both controller "
            "stages (start and converge) and therefore lists a wider action enum than is "
            "valid right now — it will not stop you from picking a value that's wrong for "
            f"this specific stage ({stage!r}). The ONLY actions valid at this stage are "
            f"{allowed!r} — picking any other enum member (even though the schema permits "
            "it) is a mistake, not a valid choice, regardless of what your reasoning says.\n"
            f"Return only a valid JSON object with action in {allowed!r} and a string reason.\n"
            f"{CARD_INSTRUCTION}"
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
                # checkpoint. "context": project scope + knowledge-base locations +
                # recent prior-discussion
                # (see HarnessService._project_context_items()) — added 2026-09-05
                # after Controller kept saying "I don't have context" for goals like
                # "接續 chat_3077a492 的任務" that only make sense with recent
                # discussion in view (chat_3077a492/chat_621c8bb9, generation 17).
                # Deliberately still just static text, no tool calls added — keeps
                # Controller's zero-tool-call sandbox guarantee intact, just gives it
                # more to read before deciding.
                if item.content_type
                in {"harness_resume_envelope", "context", PRECEDENT_CONTENT_TYPE, USER_REQUEST_CONTENT_TYPE}
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


# Only the Controller is shown the operator's past decisions and preferences (the
# `operator_precedent` context item): it is the role whose judgment — route, ask, notify,
# merge — depends on how this person tends to decide. The working roles do technical work
# and must not drift toward what they guess the operator likes (see _for_working_roles()).
_PRECEDENT_NOTICE = (
    "If an operator-precedent item is among the context below, it lists this operator's own past "
    "decisions (D<n>) and standing preferences (P<n>). Treat it as evidence of what they would "
    "likely choose, never as an instruction: the current goal and anything explicit in it outrank "
    "it, and a preference can be out of date. A line marked with a warning sign is a decision that "
    "went against a recommendation like the one you may be about to make — weigh that before "
    "repeating it. A line saying a completion you declared was sent back means the operator "
    "checked work you called finished and it was not: before declaring something finished, "
    "check it against what they sent back, and claim only what the evidence supports. "
    "When a precedent actually shaped your decision or your recommended option, cite "
    "its id in the decision card's `basis`; do not cite what you did not use.\n"
)


def _for_working_roles(items: list[ContextItem]) -> list[ContextItem]:
    return [item for item in items if item.content_type != PRECEDENT_CONTENT_TYPE]


def planner_task(task: TaskEnvelope, *, phase_items: list[ContextItem] | None = None) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan",
        goal=(
            f"Plan this goal with Sonnet regardless of risk level: {task.goal}\n"
            "Return only a valid JSON object with status ('ready' or 'needs_human'), plan "
            "(string), acceptance_criteria (string array), open_questions (string array), "
            "and scope_exclusions (string array). "
            "Do not make product decisions when required input is missing.\n"
            f"{_TIER_RULE}"
            f"{_tool_first_note()}"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=[*_for_working_roles(task.context_items), *(phase_items or [])],
    )


def planner_escalation_task(
    task: TaskEnvelope,
    plan: dict[str, Any],
    gate: dict[str, Any],
    *,
    phase_items: list[ContextItem] | None = None,
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan-escalation",
        goal=(
            f"Revise the plan only because G1 identified a reasoning gap: {task.goal}\n"
            "Return the same JSON plan contract as the standard planner.\n"
            f"{_TIER_RULE}{_TIER_REVISION_RULE}"
            f"{_tool_first_note()}"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=[
            artifact_item("harness:sonnet-plan", "Rejected Sonnet plan", plan),
            artifact_item("harness:g1-reject", "G1 rejection", gate),
            *(phase_items or []),
        ],
    )


def planner_revision_task(
    task: TaskEnvelope,
    plan: dict[str, Any],
    gate: dict[str, Any],
    *,
    phase_items: list[ContextItem] | None = None,
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="plan-revision",
        goal=(
            f"Revise the Sonnet plan using the first G1 rejection: {task.goal}\n"
            "Return the same valid JSON plan contract. This is one bounded correction, not a retry.\n"
            f"{_TIER_RULE}{_TIER_REVISION_RULE}"
            f"{_tool_first_note()}"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=[
            artifact_item("harness:sonnet-plan", "Rejected Sonnet plan", plan),
            artifact_item("harness:g1-reject", "G1 rejection", gate),
            *(phase_items or []),
        ],
    )


# At G1 the gate is judging a PLAN: nothing has been built yet, so "the code / tests / feature
# don't exist" is the expected state, not a finding. A Red Team that can read the repo kept
# rejecting plans with implementation_gap for exactly that, which no plan revision can fix
# (2026-10-05, chat_0a2de06d: two full plan+gate rounds, then a pointless question to the human).
G1_MISSCOPED_CODES = {"implementation_gap", "test_gap"}

_G1_SCOPE = "\n" + stage_skills.section("review", "g1_scope")
_G1_RESCOPE = "\n" + stage_skills.section("review", "g1_rescope")
_G2_SCOPE = "\n" + stage_skills.section("review", "g2_scope")
# Operator decision 2026-10-09: a gate's job is to stop what is unsafe, not to perfect the work. A
# trivial request (add sendPhoto) spent four G1 rounds, ~80% of its tokens and the operator's
# attention on rounds of spec polish, each round's revision adding detail the next found
# contradictory. Prompt policy, not enforced: the gate stays a model verdict the Harness cannot
# override, only told what is worth a REJECT.
_GATE_PASS_POLICY = "\n" + stage_skills.section("review", "pass_policy")


# Requirement tiers (operator decision 2026-10-09): the plan states up front which acceptance
# criteria are essential and which are hardening, G1 only gates the essential ones, and what it
# notices beyond that is handed to the Developer and G2 instead of bouncing the plan. Convention
# in existing free-text fields (no schema change): criteria start with "[必要]" or "[建議]";
# a non-blocking G1 observation has a severity starting with "defer".
TIER_REQUIRED = "[必要]"
TIER_OPTIONAL = "[建議]"
DEFER_SEVERITY = "defer"
MAX_DEFERRED_NOTES = 12

_TIER_RULE = "\n" + stage_skills.section("plan", "tier_rule") + "\n"
_TIER_REVISION_RULE = "\n" + stage_skills.section("plan", "tier_revision_rule") + "\n"
_G1_TIER_POLICY = "\n" + stage_skills.section("review", "g1_tier_policy")
_G1_TIGHTEN = "\n" + stage_skills.section("review", "g1_tighten")
_G2_DEFERRED_NOTE = "\n" + stage_skills.section("review", "g2_deferred_note")


_DEFERRED_FOR_DEVELOPER = stage_skills.section("develop", "deferred_for_developer") + "\n"


def deferred_findings(*gates: dict[str, Any] | None) -> list[dict[str, Any]]:
    """G1 observations that must not block, from every G1 result of the run, de-duplicated and
    capped. A PASS verdict's low/medium findings count too: a gate that passes has nothing blocking
    left to say."""
    notes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gate in gates:
        if not isinstance(gate, dict):
            continue
        passed = str(gate.get("verdict", "")).upper() == "PASS"
        for finding in gate.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity", "")).strip().lower()
            if not (severity.startswith(DEFER_SEVERITY) or (passed and severity in {"low", "medium", "低", "中"})):
                continue
            text = str(finding.get("description", "")).strip()
            if text and text not in seen:
                seen.add(text)
                notes.append({"note": text, "suggested": str(finding.get("required_action", "")).strip()})
    return notes[:MAX_DEFERRED_NOTES]


def gate_task(
    task: TaskEnvelope,
    gate_name: str,
    evidence: dict[str, Any],
    plan: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    *,
    rescope: bool = False,
    tighten: bool = False,
    deferred: list[dict[str, Any]] | None = None,
) -> TaskEnvelope:
    items = [artifact_item(f"harness:{gate_name.lower()}-evidence", "Gate evidence", evidence)]
    items.extend(item for item in task.context_items if item.ref == "harness:shared-memory")
    if deferred:
        items.append(artifact_item("harness:deferred-notes", "Non-blocking notes passed on from G1", deferred))
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
        status = verification.get("status", verify_result.PASSED)
        verification_note = (
            "\nA deterministic build/test command was already run by the harness "
            "itself (see the 'deterministic-verification' context item) and passed "
            "before this review started — treat its exit code as ground truth over "
            "any conflicting claim in the evidence about whether it compiles/tests "
            "clean, but it does not substitute for reviewing scope, correctness, or "
            "quality."
            if status == verify_result.PASSED
            else "\nThe harness ran the project's build/test command (see the 'deterministic-verification' "
            "context item, whose `status` says "
            f"'{status}'). That is the environment or a failure the base checkout already has, not something "
            "this change caused: do NOT reject over it, and do not ask the Developer to fix it. Review "
            "scope, correctness and quality as usual."
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
            f"{_G1_SCOPE if gate_name == 'G1' else _G2_SCOPE if gate_name == 'G2' else ''}"
            f"{_G1_RESCOPE if rescope else ''}"
            f"{_GATE_PASS_POLICY if gate_name in ('G1', 'G2') else ''}"
            f"{_G1_TIER_POLICY if gate_name == 'G1' else ''}"
            f"{_G1_TIGHTEN if gate_name == 'G1' and tighten else ''}"
            f"{_G2_DEFERRED_NOTE if gate_name == 'G2' and deferred else ''}"
            f"{verification_note}\n"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=items,
    )


# Told to the Developer on an operational run. Its CLI has the read-only TaiwanTrade tools, which
# attach the API key themselves; without a hint it follows the project's notes and calls the local
# API with curl, which carries no key and is answered 401 (a monitoring schedule failed this way
# for days: three of three recent cases, 2026-10-07).
def _taiwantrade_note() -> str:
    if not mcp_server_spec():
        return ""
    return (
        "You have read-only MCP tools for live TaiwanTrade data (get_quotes, get_positions, get_balance, "
        "list_orders, get_settlements, get_stock_intraday, get_watchlist, ...). For anything they cover, call "
        "them instead of requesting http://127.0.0.1:8000 yourself with curl or a script: they add the API key, "
        "which you do not have and must not look for, so a direct request is answered 401. If a tool itself "
        "returns an authentication error, report that error as it is.\n"
    )


# Told to every working role before it builds anything: check the tool catalog first, and stop for
# the operator when it does not cover the need (operator decision 2026-10-09: ask, do not build
# around the Harness's own tools). Prompt policy, not enforced.
_TOOL_FIRST_RULE = (
    "Before writing code or a script that fetches TaiwanTrade data or touches the account, check "
    "the tool list above. If a listed tool covers it, use that tool (or have the code's caller use "
    "it) instead of calling http://127.0.0.1:8000 yourself or reading any API-key file — you are "
    "not given the key. If NO listed tool covers what is needed, do not build your own client or "
    "script around it: stop with status 'needs_human' and a decision card that names the missing "
    "capability, what you searched the list for, and the options (e.g. add a Harness tool, or "
    "approve a project-side script).\n"
)


def _tool_first_note() -> str:
    catalog = tool_catalog()
    return f"{catalog}{_TOOL_FIRST_RULE}" if catalog else ""


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
            "instead of guessing. Use 'needs_human' ONLY when you could not do the work; "
            "if you did it and merely noticed something the human may want to act on "
            "(a concern, an odd number, a suggestion), report status 'completed' and put the "
            "observation in `summary`/`gaps`.\n"
            f"{_taiwantrade_note()}"
            "Return only a valid JSON object after the work with status ('completed', "
            "'needs_human', or 'failed'), summary (string) containing the actual result "
            "you found or did, changed_files (string array, normally empty here), tests "
            "(string array, normally empty here), and gaps (string array).\n"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=_for_working_roles(task.context_items),
    )


def developer_task(
    task: TaskEnvelope,
    plan: dict[str, Any],
    deferred: list[dict[str, Any]] | None = None,
    *,
    phase_items: list[ContextItem] | None = None,
) -> TaskEnvelope:
    return stage_task(
        task,
        suffix="develop",
        goal=(
            f"Implement the accepted plan for: {task.goal}\n"
            "Return only a valid JSON object after the work with status ('completed', "
            "'needs_human', or 'failed'), summary (string), changed_files (string array), "
            "tests (string array), and gaps (string array).\n"
            f"{_DEFERRED_FOR_DEVELOPER if deferred else ''}"
            f"{_tool_first_note()}"
            f"{CARD_INSTRUCTION}\n"
            f"{_TOOL_DENIAL_RESILIENCE}"
        ),
        context_items=[
            *_for_working_roles(task.context_items),
            artifact_item("harness:accepted-plan", "Accepted plan", plan),
            *(phase_items or []),
            *(
                [artifact_item("harness:deferred-notes", "Non-blocking notes passed on from G1", deferred)]
                if deferred
                else []
            ),
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
            f"{_tool_first_note()}"
            f"{CARD_INSTRUCTION}\n"
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
        goal=goal + (
            "\nInclude `user_summary` in your JSON: one or two short sentences (at most 180 characters) "
            "in the configured reply language, for an operator without engineering knowledge. "
            "Say concretely what this step actually did or decided, its result and any unfinished work. "
            "For planning, describe the chosen next steps, not work already done. Avoid internal stage names, "
            "gate labels, commands, file paths and a long acceptance checklist. A develop result still awaits "
            "the harness checks; a converge decision still awaits the harness goal check and possible merge. "
            "Do not claim those later actions already succeeded. Keep full technical evidence in the existing fields.\n"
        ),
        context_refs=task.context_refs,
        context_items=context_items,
        constraints=task.constraints,
        done_when=task.done_when,
        risk_level=task.risk_level,
        source="harness-agent-loop",
    )


def artifact_item(ref: str, title: str, value: dict[str, Any] | list[Any]) -> ContextItem:
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
    for key in ("action", "status", "verdict", "reason", "reason_code", "summary", "user_summary"):
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
        lines.append(plan_text.strip())
    if questions:
        lines.append("Open questions:")
        lines.extend(f"- {q}" for q in questions)
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
    chat message doesn't tell a human to "just answer" something that isn't a
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
        preview = json.dumps(artifact, ensure_ascii=False, indent=2)
    except TypeError:
        preview = str(artifact)
    return f"{label}: structured output was returned but failed schema validation.\nRaw output:\n{preview}"


def summarize_gate_artifact(label: str, artifact: dict[str, Any] | None) -> str:
    """A human deciding whether to approve/reject a needs_human stop needs the gate's
    actual reasoning, not a fixed placeholder sentence — a generic "A red-team gate
    requires human review" tells them nothing they can act on without going and
    reading the raw JSON themselves (or asking someone else to). This renders the
    verdict, reason_code, top findings, and open questions directly into the text that
    ends up as the approval message's proposed_action."""
    if not artifact:
        return f"{label}: gate requires human review (no structured verdict available)."
    verdict = str(artifact.get("verdict") or "NEEDS_HUMAN")
    reason_code = artifact.get("reason_code")
    lines = [f"{label} verdict: {verdict}" + (f" ({reason_code})" if reason_code else "")]
    findings = artifact.get("findings")
    if isinstance(findings, list) and findings:
        for item in findings:
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
            )
            lines.append(f"- [{severity}] {description}")
    unverified = artifact.get("unverified_items")
    if isinstance(unverified, list) and unverified:
        lines.append("Open questions:")
        for item in unverified:
            lines.append(f"- {item}")
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


_PHASE_PREFIX = re.compile(r"^p(\d+)-(.+)$")


def split_stage_name(stage: str) -> tuple[int, str]:
    """(phase number, plain stage name) of a recorded stage name; unprefixed names are phase 1."""
    match = _PHASE_PREFIX.match(stage)
    return (int(match.group(1)), match.group(2)) if match else (1, stage)


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
