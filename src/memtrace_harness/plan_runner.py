"""Runs the multi-step plans a chat reply starts (plans.py), one step at a time, from the Harness tick.

What it does for a plan: create it from the model's ``HARNESS_PLAN::`` line, start each step through the same doors a
single request uses (a ``read`` tool runs at once; ``run_job`` and ``change`` tools become ordinary confirm cards that the
user taps or the allowed list answers), check each result against the step's expectation, keep one progress message
up to date (edited in place), and say at the end whether the goal was reached.

Boundaries (all enforced): one unfinished plan per project; at most 10 steps; a step is a registered tool or a declared
job, never a code change (that is the Agent Loop's); the plan-wide "核准所有步驟" button only auto-answers the confirm cards
of steps that were in the plan when it was created and still never covers a command the command policy forbids; a step
that fails or whose expectation is not met stops the plan and asks the user (retry / skip / cancel) rather than carrying on;
a result the Harness cannot judge counts as not met. State is in the store, so a restart resumes the plan: a read step
that was running is run again, a step waiting on a confirm card keeps waiting, a result waiting to be judged is judged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from memtrace_harness import plans, tool_registry
from memtrace_harness.command_policy import forbidden_reason

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
PLAN_LIFETIME = timedelta(hours=24)
REQUEST_TTL_SECONDS = 600
NOTE_CHARS = 200


def allowed_step_tool(name: str) -> str | None:
    """Why a tool cannot be a plan step, or None."""
    if name == "run_job":
        return None
    spec = tool_registry.get(name)
    if spec is None:
        return "沒有這個工具"
    if spec.tier == tool_registry.PROPOSE:
        return "這個工具只用來向你提出要求，不能當步驟"
    return None


def _short(text: str, limit: int = NOTE_CHARS) -> str:
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


class PlanRunner:
    def __init__(self, service: Any) -> None:
        self.service = service
        self._live: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    @property
    def store(self) -> Any:
        return self.service.approval_manager.trace_store

    # ---- creating
    def create(self, scope: Any, audience: str | None, payload: str) -> str | None:
        """Start a plan from a marker payload. None when it started; otherwise the message to show instead."""
        try:
            plan = plans.parse_plan(payload, allowed_tool=allowed_step_tool)
        except plans.PlanError as exc:
            return f"⚠️ 這個多步驟計畫沒有建立：{exc}"
        for number, step in enumerate(plan["steps"], 1):
            if step["tool"] == "run_job":
                command = (scope.jobs or {}).get(str(step["args"].get("name")))
                if command is None:
                    return f"⚠️ 這個多步驟計畫沒有建立：第 {number} 步的工作 {step['args'].get('name')!r} 沒有在專案設定裡"
                reason = forbidden_reason(command)
                if reason:
                    return f"⚠️ 這個多步驟計畫沒有建立：第 {number} 步的工作是「{reason}」，{self._hint()}"
        plan_id = f"plan_{uuid.uuid4().hex[:10]}"
        if not self.store.create_plan(
            plan_id=plan_id, project=scope.name, goal=plan["goal"], steps=plan["steps"], audience=audience
        ):
            return "⚠️ 目前已經有一個進行中的計畫，請等它結束或先取消，才能開新的。"
        return None

    @staticmethod
    def _hint() -> str:
        from memtrace_harness.command_policy import REFUSAL_HINT

        return REFUSAL_HINT

    # ---- the tick
    def tick(self) -> None:
        for plan in self.store.list_active_plans():
            try:
                if plan["status"] == plans.P_RUNNING:
                    self._advance(plan)
            except Exception:
                logger.exception(f"advancing plan {plan['plan_id']} failed; continuing")
        self._push_views()

    def _advance(self, plan: dict) -> None:
        steps = self.store.list_plan_steps(plan["plan_id"])
        created = datetime.fromisoformat(plan["created_at"])
        if datetime.now(timezone.utc) - created > PLAN_LIFETIME:
            self._cancel(plan, steps, "超過 24 小時，計畫自動取消。")
            return
        current = next((s for s in steps if s["status"] not in {plans.DONE, plans.SKIPPED}), None)
        if current is None:
            self.store.update_plan(plan["plan_id"], status=plans.P_DONE)
            self.service.send_message(plan["audience"], plans.render_final({**plan, "status": plans.P_DONE}, steps))
            return
        status = current["status"]
        if status == plans.FAILED:
            self.store.update_plan(plan["plan_id"], status=plans.P_ATTENTION)
        elif status == plans.PENDING:
            self._start(plan, current)
        elif status == plans.AWAITING:
            self._poll_request(plan, current)
        elif status in {plans.RUNNING, plans.CHECKING}:
            with self._lock:
                alive = (plan["plan_id"], current["idx"]) in self._live
            if not alive:  # the Harness restarted while this was in flight
                if status == plans.RUNNING:
                    self.store.transition_plan_step(plan["plan_id"], current["idx"], (plans.RUNNING,), plans.PENDING)
                else:
                    self._check(plan, current, current["result_text"] or "")

    # ---- starting a step
    def _scope(self, project: str) -> Any:
        return next((p for p in self.service.projects if p.name == project), None)

    def _fail(self, step: dict, note: str) -> None:
        self.store.transition_plan_step(
            step["plan_id"], step["idx"],
            (plans.PENDING, plans.RUNNING, plans.AWAITING, plans.CHECKING), plans.FAILED,
            note=_short(note), finished_at=datetime.now(timezone.utc).isoformat(),
        )

    def _start(self, plan: dict, step: dict) -> None:
        scope = self._scope(plan["project"])
        if scope is None:
            self._fail(step, "找不到這個專案")
            return
        if step["attempts"] >= MAX_ATTEMPTS:
            self._fail(step, f"已重試 {MAX_ATTEMPTS} 次仍沒有成功")
            return
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat()
        if step["tool"] == "run_job":
            name = str(step["args"].get("name"))
            command = (scope.jobs or {}).get(name)
            if command is None:
                self._fail(step, f"工作 {name} 已不在專案設定裡")
                return
            reason = forbidden_reason(command)
            if reason:
                self._fail(step, f"{reason}。{self._hint()}")
                return
            rid = f"job_{uuid.uuid4().hex[:10]}"
            self.store.create_job_request(request_id=rid, project=scope.name, job_name=name, command=command, expires_at=expires)
            row = self.store.get_job_request(rid) or self.store.find_active_job_request(scope.name, name)
        else:
            spec = tool_registry.get(step["tool"])
            if spec is None:
                self._fail(step, f"工具 {step['tool']} 已不存在")
                return
            if spec.tier == tool_registry.READ:
                self._start_read(plan, step, scope, spec)
                return
            from memtrace_harness.ops_mcp import ops_context_for_scope

            try:
                args = spec.validate(ops_context_for_scope(scope, self.store), step["args"]) if spec.validate else dict(step["args"])
            except ValueError as exc:
                self._fail(step, f"參數不對：{exc}")
                return
            summary = spec.summarize(args) if spec.summarize else f"{spec.name} {json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            rid = f"tool_{uuid.uuid4().hex[:10]}"
            self.store.create_tool_call_request(
                request_id=rid, project=scope.name, tool=spec.name, arguments=args, summary=summary, expires_at=expires
            )
            key = f"tool:{spec.name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            row = self.store.get_job_request(rid) or self.store.find_active_job_request(scope.name, key)
        if row is None:
            self._fail(step, "沒能建立確認要求")
            return
        self.store.transition_plan_step(
            plan["plan_id"], step["idx"], (plans.PENDING,), plans.AWAITING,
            request_id=row["request_id"], attempts=step["attempts"] + 1, started_at=now.isoformat(),
        )

    def _start_read(self, plan: dict, step: dict, scope: Any, spec: tool_registry.ToolSpec) -> None:
        if not self.store.transition_plan_step(
            plan["plan_id"], step["idx"], (plans.PENDING,), plans.RUNNING,
            attempts=step["attempts"] + 1, started_at=datetime.now(timezone.utc).isoformat(),
        ):
            return
        key = (plan["plan_id"], step["idx"])
        with self._lock:
            self._live.add(key)

        def run() -> None:
            try:
                from memtrace_harness.ops_mcp import ops_context_for_scope

                ctx = ops_context_for_scope(scope, self.store)
                if spec.name not in ctx.tool_names():
                    raise tool_registry.OpsError(f"工具 {spec.name} 在這個專案不可用")
                text = spec.handler(ctx, step["args"])
            except Exception as exc:  # noqa: BLE001 — a failed step is reported, not raised
                self._fail(step, str(exc))
            else:
                self._after_result(plan, step, text)
            finally:
                with self._lock:
                    self._live.discard(key)

        threading.Thread(target=run, daemon=True, name=f"plan-{plan['plan_id']}-{step['idx']}").start()

    # ---- results
    def _poll_request(self, plan: dict, step: dict) -> None:
        row = self.store.get_job_request(step["request_id"]) if step["request_id"] else None
        if row is None:
            self._fail(step, "找不到對應的確認要求")
            return
        status = row["status"]
        if status in {"pending", "awaiting", "running"}:
            return
        result = row.get("result") or {}
        text = str(result.get("output_tail") or result.get("error") or "")
        if status == "done":
            self._after_result(plan, step, text)
        elif status == "cancelled":
            self._fail(step, "你取消了這一步")
        elif status == "expired":
            self._fail(step, "確認逾時，沒有執行")
        else:
            self._fail(step, text or "執行失敗")

    def _after_result(self, plan: dict, step: dict, text: str) -> None:
        if not self.store.transition_plan_step(
            plan["plan_id"], step["idx"], (plans.RUNNING, plans.AWAITING), plans.CHECKING, result_text=text[:6000]
        ):
            return  # the plan was cancelled (or the step reset) while this was running: its result is dropped
        self._check(plan, {**step, "status": plans.CHECKING}, text)

    def _check(self, plan: dict, step: dict, text: str) -> None:
        achieved, reason = plans.check_expectation(step["expect"], text)
        if achieved is None:
            key = (plan["plan_id"], step["idx"])
            with self._lock:
                if key in self._live:
                    return
                self._live.add(key)

            def judge() -> None:
                try:
                    verdict, why = self.service.judge_plan_step(self._scope(plan["project"]), plan, step, text)
                except Exception as exc:  # noqa: BLE001 — a judgement that cannot be made counts as not met
                    verdict, why = False, f"無法自動判斷（{exc}）"
                finally:
                    with self._lock:
                        self._live.discard(key)
                self._finish_check(plan, step, verdict, why, text)

            threading.Thread(target=judge, daemon=True, name=f"plan-judge-{plan['plan_id']}-{step['idx']}").start()
            return
        self._finish_check(plan, step, achieved, reason, text)

    def _finish_check(self, plan: dict, step: dict, achieved: bool | None, reason: str, text: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if achieved:
            snippet = _short(text, 120)
            self.store.transition_plan_step(
                plan["plan_id"], step["idx"], (plans.CHECKING,), plans.DONE,
                note=_short(reason + (f"：{snippet}" if snippet else "")), finished_at=now,
            )
        else:
            self.store.transition_plan_step(
                plan["plan_id"], step["idx"], (plans.CHECKING,), plans.FAILED,
                note=_short(f"沒達到預期：{reason}"), finished_at=now,
            )

    # ---- what the user can tap
    def handle_action(self, action: str, plan_id: str) -> str:
        plan = self.store.get_plan(plan_id)
        if plan is None:
            return "找不到這個計畫。"
        steps = self.store.list_plan_steps(plan_id)
        if plan["status"] not in {plans.P_RUNNING, plans.P_ATTENTION}:
            return "這個計畫已經結束了。"
        if action == "plan_cancel":
            self._cancel(plan, steps, "你取消了計畫。")
            return "已取消計畫。"
        if action == "plan_approve_all":
            self.store.update_plan(plan_id, approve_all=True)
            return "已核准這個計畫的所有步驟。"
        failed = next((s for s in steps if s["status"] == plans.FAILED), None)
        if failed is None:
            return "目前沒有失敗的步驟。"
        if action == "plan_retry":
            self.store.transition_plan_step(plan_id, failed["idx"], (plans.FAILED,), plans.PENDING)
        elif action == "plan_skip":
            self.store.transition_plan_step(plan_id, failed["idx"], (plans.FAILED,), plans.SKIPPED)
        else:
            return "不認得這個動作。"
        self.store.update_plan(plan_id, status=plans.P_RUNNING)
        return "好，繼續。"

    def _cancel(self, plan: dict, steps: list[dict], note: str) -> None:
        for step in steps:
            if step["status"] in {plans.PENDING, plans.AWAITING, plans.RUNNING, plans.CHECKING, plans.FAILED}:
                if step["request_id"]:
                    row = self.store.get_job_request(step["request_id"])
                    if row and row["status"] in {"pending", "awaiting"}:
                        if self.store.transition_job_request(step["request_id"], ("pending", "awaiting"), "cancelled"):
                            self.store.finish_job_request(step["request_id"], "cancelled")
                            self.service.clear_actions(row["message_ref"])
                self.store.transition_plan_step(
                    plan["plan_id"], step["idx"],
                    (plans.PENDING, plans.AWAITING, plans.RUNNING, plans.CHECKING, plans.FAILED), plans.SKIPPED, note=note,
                )
        self.store.update_plan(plan["plan_id"], status=plans.P_CANCELLED)

    def auto_approves(self, request_id: str) -> bool:
        """Has the user approved every step of the plan this confirm card belongs to (and is it still running)?"""
        plan = self.store.plan_for_request(request_id)
        return bool(plan and plan["approve_all"] and plan["status"] == plans.P_RUNNING)

    # ---- the one progress message
    def _push_views(self) -> None:
        for plan in self.store.list_plans_to_show():
            steps = self.store.list_plan_steps(plan["plan_id"])
            text, keyboard = plans.render_progress(plan, steps)
            digest = hashlib.sha256((text + json.dumps(keyboard)).encode("utf-8")).hexdigest()[:16]
            if digest == plan["rendered_hash"]:
                continue
            if not plan["message_sent"]:
                self.service.send_message_with_keyboard(
                    plan["audience"], text, keyboard, bind={"type": "plan", "id": plan["plan_id"]}
                )
                self.store.update_plan(plan["plan_id"], message_sent=True, rendered_hash=digest)
            elif plan["message_ref"]:
                self.service.edit_message(plan["message_ref"], text, keyboard)
                self.store.update_plan(plan["plan_id"], rendered_hash=digest)
            # else: sent but not yet delivered — the next tick shows the newest state once we have its reference

    def on_delivery(self, plan_id: str, *, delivered: bool, ref: str | None) -> None:
        if delivered and ref:
            self.store.update_plan(plan_id, message_ref=ref)  # a newer state, if any, is edited in on the next tick
        elif not delivered:
            self.store.update_plan(plan_id, message_sent=False, rendered_hash=None)
