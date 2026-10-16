from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from unittest import TestCase
from unittest.mock import MagicMock, patch

from memtrace_harness.cli_process import ProcessResult
from memtrace_harness.goal_plan import judge_goal_check, parse_goal_plan
from memtrace_harness.loop import AgentLoopRunner, controller_task
from memtrace_harness.plan_runner import PlanRunner
from memtrace_harness.primary_agent import PrimaryAgent, SessionAdapter
from memtrace_harness.role_profiles import load_role_profiles
from memtrace_harness.schemas import LoopSummary, TaskEnvelope, TokenUsage
from tests.test_loop import QueueAdapter, completed_development
from tests.test_service import _build_schedule_gateway


class PrimaryAgentTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway, self.store, self.scope = _build_schedule_gateway(Path(self.tmp.name))
        self.scope = self.gateway.projects[0]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "HARNESS_CHAT_PROVIDER": "claude", "HARNESS_CHAT_MODEL": "sonnet",
            "HARNESS_CHAT_FALLBACKS": "codex/gpt-test",
        }).start()

    def test_dialogue_and_decisions_use_one_chain_without_changing_skill_permissions(self):
        profiles = load_role_profiles()
        result = self.gateway.primary_agent.profiles(profiles, self.scope.name)
        self.assertEqual(
            [(c.provider, c.model) for c in result["controller"].candidates],
            list(self.gateway.primary_agent.candidates(self.scope.name)),
        )
        self.assertEqual(result["controller"].context_policy, "primary-session")
        self.assertEqual(result["controller"].permission, "read-only")
        self.assertIs(result["develop"], profiles["develop"])
        self.assertIs(result["review"], profiles["review"])
        self.assertEqual(profiles["controller"].context_policy, "loop-snapshot")

    def test_every_decision_rehydrates_new_discussion_without_leaking_it_into_review(self):
        context = ["first user request"]
        recorded = []
        delegate = QueueAdapter("controller", "claude", "sonnet", [
            {"action": "next_phase", "reason": "first"},
            {"action": "finish", "reason": "second"},
        ])
        review = QueueAdapter("review", "codex", "review-model", [])
        adapters = PrimaryAgent.bind(
            {"controller": [delegate], "review": [review]},
            context=lambda: context[0], record=lambda text, response: recorded.append(text),
        )
        task = TaskEnvelope("t", "ws", "do the work")
        adapters["controller"][0].run(task, "trace/1")
        context[0] = "user clarified the acceptance criteria"
        adapters["controller"][0].run(task, "trace/2")
        self.assertIn("first user request", delegate.calls[0].context_items[-1].body)
        self.assertIn("clarified", delegate.calls[1].context_items[-1].body)
        self.assertEqual(len(recorded), 2)
        self.assertIs(adapters["review"][0], review)

    def test_service_task_decisions_continue_the_project_conversation(self):
        scope = self.scope
        scope.loop_mode = "lean"
        scope.verify_command = None
        goal_plan = {
            "restated_goal": "fix the service", "success_criteria": ["service fixed"],
            "phases": [{"title": "Fix", "purpose": "fix the service", "skills": ["develop"], "acceptance": []}],
        }
        owner = QueueAdapter("controller", "claude", "sonnet", [
            {"action": "run_planner", "reason": "implement", "goal_plan": goal_plan},
            {"action": "finish", "reason": "verified", "goal_check": {
                "verdict": "met", "criteria": [{"criterion": "service fixed", "status": "met", "evidence": "checked"}],
                "remaining": [],
            }},
        ])
        developer = QueueAdapter("develop", "antigravity", "gemini-test", [completed_development()])
        original_run = developer.run

        def develop(task, trace):
            self.gateway.primary_session_mgr.record_turn(
                project=scope.name, speaker="user", turn_type="discussion", content="新的補充：保留相容性",
            )
            return original_run(task, trace)

        developer.run = develop
        candidates = {p: [QueueAdapter(p, "claude", "unused", [])] for p in load_role_profiles()}
        candidates.update(controller=[owner], develop=[developer])
        with patch("memtrace_harness.service.build_role_adapter_candidates", return_value=candidates) as build, \
             patch.object(self.gateway, "_project_context_items", return_value=[]), \
             patch.object(self.gateway, "_provision_workdir", return_value=(scope.working_directory, None)), \
             patch.object(self.gateway, "_identity_context", side_effect=lambda s: self.gateway.primary_session_mgr.get_rehydration_context(s.name)):
            summary = self.gateway._run_new_task(scope, "primary_task", "fix the service", request="請修好服務")
        self.assertEqual(summary.status, "succeeded")
        self.assertEqual(build.call_args.kwargs["profiles"]["controller"].model, "sonnet")
        self.assertIn("新的補充", owner.calls[-1].context_items[-1].body)
        self.assertNotIn("neutral, empty sandbox", owner.calls[0].goal)
        self.assertFalse(any(i.ref == "harness:primary-session" for i in developer.calls[0].context_items))
        turns = self.store.get_primary_session_turns(f"psess_{scope.name}")
        self.assertEqual(sum(t["turn_type"] == "agent_decision" for t in turns), 2)
        self.assertTrue(any(t["turn_type"] == "agent_progress" for t in turns))

    def test_raw_dialogue_attempt_is_local_and_usage_is_unavailable(self):
        process = ProcessResult(["claude", "--print", "private prompt"], 0, "raw reply", "diagnostic",
                                "2026-10-15T00:00:00+00:00", "2026-10-15T00:00:01+00:00", 1000)
        self.gateway.primary_agent.record_dialogue(self.store, "ws", "hello", "claude", "sonnet", process)
        with self.store._connection() as conn:
            summary = json.loads(conn.execute("SELECT summary_json FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
        execution = summary["responses"][0]["execution"]
        self.assertEqual(execution["usage"]["completeness"], "unavailable")
        self.assertEqual(Path(execution["raw_trace_ref"]).read_text(), "raw reply")
        self.assertEqual(execution["command"][-1], "<task-prompt>")

    def test_primary_decision_prompt_uses_the_same_agent_and_legacy_replay_still_works(self):
        task = TaskEnvelope("t", "ws", "fix")
        primary = controller_task(task, stage="start", stages=[], primary_agent=True)
        legacy = controller_task(task, stage="start", stages=[])
        self.assertIn("single primary agent", primary.goal)
        self.assertIn("neutral, empty sandbox", legacy.goal)

    def test_cancellation_saves_the_executed_attempt_and_stops_before_the_next_model_call(self):
        cancelled = [False]
        owner = QueueAdapter("controller", "claude", "sonnet", [{
            "action": "run_planner", "reason": "work", "goal_plan": {
                "restated_goal": "g", "success_criteria": ["fixed"],
                "phases": [{"title": "fix", "purpose": "fix", "skills": ["develop"]}],
            },
        }])
        develop = QueueAdapter("develop", "antigravity", "m", [completed_development()])
        run = develop.run
        def cancel_after_develop(task, trace):
            response = run(task, trace)
            cancelled[0] = True
            return response
        develop.run = cancel_after_develop
        adapters = {p: QueueAdapter(p, "claude", "unused", []) for p in load_role_profiles()}
        adapters.update(controller=owner, develop=develop)
        summary = AgentLoopRunner(
            adapters=adapters, trace_store=self.store, loop_mode="lean",
            cancel_check=lambda: cancelled[0],
        ).run(TaskEnvelope("t", "ws", "fix"), conversation_id="cancelled_task")
        self.assertEqual(summary.status, "failed")
        self.assertEqual([s.stage for s in summary.stages], ["control-start", "develop"])
        self.assertEqual(self.store.conversation_outcome("cancelled_task")["status"], "failed")
        self.assertEqual(len(owner.calls), 1)

    def test_goal_checks_cannot_repeat_or_substitute_a_criterion_or_leave_it_unverified(self):
        plan = parse_goal_plan({
            "restated_goal": "g", "success_criteria": ["a", "b"],
            "phases": [{"title": "x", "purpose": "x", "skills": ["develop"]}],
        })
        for entries in [
            [("a", "met"), ("a", "met")],
            [("a", "met"), ("other", "met")],
            [("a", "met"), ("b", "unverified")],
        ]:
            check = {"verdict": "met", "criteria": [{"criterion": n, "status": s} for n, s in entries]}
            self.assertFalse(judge_goal_check(plan, check).complete)


class MixedPlanTests(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway, self.store, self.scope = _build_schedule_gateway(Path(self.tmp.name))
        self.scope = self.gateway.projects[0]
        self.scope.working_directory = Path(self.tmp.name)
        self.scope.jobs = {"check": "echo verified"}
        self.gateway._drain_task_queue = MagicMock()

    def create(self, steps):
        self.assertIsNone(self.gateway.plan_runner.create(self.scope, None, json.dumps({"goal": "finish whole request", "steps": steps})))
        return self.store.list_active_plans()[0]["plan_id"]

    def step(self, plan_id):
        return self.store.list_plan_steps(plan_id)[0]

    def join(self):
        for thread in threading.enumerate():
            if thread.name.startswith(("plan-develop-", "job-")):
                thread.join(5)

    def test_development_is_a_step_and_following_operation_waits_for_its_real_result(self):
        entered, release = threading.Event(), threading.Event()

        def develop(scope, conv, goal, **kwargs):
            entered.set()
            release.wait(5)
            task = TaskEnvelope("t", scope.workspace_id, goal)
            self.store.create_conversation(task, conv)
            trace = self.store.create_run(task, conversation_id=conv)
            summary = LoopSummary(task, [], "succeeded", "implemented", trace, TokenUsage(), conversation_id=conv)
            self.store.save_loop_summary(summary)
            return summary

        self.gateway._run_new_task = develop
        pid = self.create([
            {"title": "implement", "tool": "develop", "args": {"goal": "make the change"}},
            {"title": "check", "tool": "run_job", "args": {"name": "check"}},
        ])
        try:
            self.gateway.plan_runner.tick()
            self.assertTrue(entered.wait(2))
            self.gateway.plan_runner.tick()
            self.assertEqual([s["status"] for s in self.store.list_plan_steps(pid)], ["running", "pending"])
        finally:
            release.set()
            self.join()
        self.gateway.plan_runner.tick()
        self.assertEqual(self.step(pid)["status"], "done")
        self.gateway.plan_runner.tick()
        self.assertEqual(self.store.list_plan_steps(pid)[1]["status"], "awaiting")

    def test_interrupted_development_is_not_blindly_replayed(self):
        pid = self.create([{"title": "implement", "tool": "develop", "args": {"goal": "change"}}])
        self.store.transition_plan_step(pid, 1, ("pending",), "running", request_id="lost_run", attempts=1)
        self.gateway._run_new_task = MagicMock()
        PlanRunner(self.gateway).tick()
        self.gateway._run_new_task.assert_not_called()
        self.assertEqual(self.step(pid)["status"], "failed")

    def test_install_is_one_waiting_step_and_is_not_completed_when_only_proposed(self):
        pid = self.create([
            {"title": "install", "tool": "request_install", "args": {"kind": "pip", "name": "example", "reason": "needed"}},
            {"title": "check", "tool": "run_job", "args": {"name": "check"}},
        ])
        self.gateway.plan_runner.tick()
        step = self.step(pid)
        self.assertEqual(step["status"], "awaiting")
        self.assertEqual(self.store.get_job_request(step["request_id"])["kind"], "install")
        self.assertEqual(self.store.list_plan_steps(pid)[1]["status"], "pending")

    def test_a2a_waits_through_carriage_and_uses_the_remote_result(self):
        self.store.replace_peer_directory([{"name": "peer", "description": "other", "collaborate": False}])
        pid = self.create([{"title": "ask", "tool": "ask_peer", "args": {"peer": "peer", "question": "q"},
                            "expect": {"kind": "contains", "value": "remote evidence"}}])
        self.gateway.plan_runner.tick()
        rid = self.step(pid)["request_id"]
        self.gateway.process_job_requests()
        self.gateway._resolve_job_action("job_confirm", rid, None, None)
        self.join()
        self.gateway.plan_runner.tick()
        peer_id = self.step(pid)["request_id"].removeprefix("peer:")
        self.assertEqual(self.step(pid)["status"], "awaiting")
        self.store.claim_peer_outbound(1)
        PlanRunner(self.gateway).tick()  # restart while the carrier is working
        self.assertEqual(self.step(pid)["status"], "awaiting")
        self.store.finish_peer_outbound(peer_id, "completed", result_text="remote evidence", reason=None)
        self.gateway.plan_runner.tick()
        self.assertEqual(self.step(pid)["status"], "done")
        self.assertIn("remote evidence", self.step(pid)["result_text"])

    def test_changed_command_invalidates_the_pending_confirmation(self):
        pid = self.create([{"title": "check", "tool": "run_job", "args": {"name": "check"}}])
        self.gateway.plan_runner.tick()
        self.gateway.process_job_requests()
        self.scope.jobs["check"] = "echo changed"
        self.gateway._run_job = MagicMock()
        message = self.gateway._resolve_job_action("job_confirm", self.step(pid)["request_id"], None, None)
        self.assertIn("確認失效", message)
        self.gateway._run_job.assert_not_called()

    def test_cancelling_a_plan_invalidates_its_development_clarification(self):
        pid = self.create([{"title": "implement", "tool": "develop", "args": {"goal": "change"}}])
        task = TaskEnvelope("t", self.scope.workspace_id, "change")
        self.store.create_conversation(task, "held_develop")
        self.store.transition_plan_step(pid, 1, ("pending",), "awaiting", request_id="held_develop")
        req = self.gateway.approval_manager.request_approval(
            conversation_id="held_develop", workspace=self.scope.workspace_id,
            working_directory=str(self.scope.working_directory), reason="ambiguous_requirement",
            proposed_action="choose requirement", resume_goal="change",
        )
        self.gateway.plan_runner.handle_action("plan_cancel", pid)
        self.assertEqual(self.gateway.approval_manager.get_request(req.id).status, "rejected")
        self.gateway._launch_resume(req)  # a queued/late resume cannot revive a cancelled plan
        self.assertIsNone(self.store.get_workspace_lock(self.scope.workspace_id))
