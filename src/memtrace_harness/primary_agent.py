"""One project agent for dialogue and task decisions, backed only by provider CLIs.

The historical ``chat`` settings are the primary agent's settings. ``controller`` is
still the persisted execution slot understood by old checkpoints, not another agent.
Skills retain their own profiles and their isolated contexts.
"""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from uuid import uuid4
from typing import Callable

from memtrace_harness.adapters.base import ModelAdapter
from memtrace_harness.config import HarnessConfig
from memtrace_harness.role_profiles import ModelCandidate, RoleProfile
from memtrace_harness.schemas import CliExecution, ContextItem, HarnessSummary, ModelResponse, TaskEnvelope

logger = logging.getLogger(__name__)


PRIMARY_AGENT_POLICY = (
    "你是這個專案唯一的主代理，持續負責與使用者對話、理解完整目標、規劃步驟、選擇能力、"
    "追蹤執行與驗收結果。對話、探索、操作和開發都是同一份工作的部分；開發是你呼叫的能力，"
    "任務中的規劃與完成判斷也由你接續負責。使用者的新補充與決定要納入目前工作脈絡。\n"
    "多步驟工作先說明計畫，再逐步執行；每一步依結果決定下一步，回報已完成、正在做、"
    "等待什麼與下一步。唯讀探索可自行進行；需要授權的操作每次單獨確認，確認只涵蓋當次具體內容，"
    "不能核准整份計畫或永久允許。缺能力時先探索現有工具與替代方法，再提出取得或製作能力的步驟。"
    "排隊、提出安裝或送出 A2A 請求都不等於工作完成；以實際結果驗收原始目標。"
    "Harness 的權限與安全檢查由程式執行，只有 develop 能力可以修改專案。\n"
)


class PrimaryAgent:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config

    def candidates(self, project: str) -> tuple[tuple[str, str], ...]:
        return self.config.chat_candidates_for(project)

    def dialogue_command(self, provider: str, model: str, prompt: str, **kwargs) -> list[str]:
        return self.config.chat_command(provider, model, prompt, **kwargs)

    def record_dialogue(self, store, workspace: str, text: str, provider: str, model: str, process, *, source="chat", context_policy="primary-session") -> None:
        """Preserve every dialogue attempt locally, with unavailable usage for plain replies."""
        from memtrace_harness.adapters.cli import redact_command
        from memtrace_harness.execution_sources import split_ref
        provider, source_id = split_ref(provider)

        task = TaskEnvelope(task_id=f"primary_{uuid4().hex[:12]}", workspace_id=workspace, goal=text, source=source)
        trace_id = store.create_run(task)
        directory = self.config.trace_root / trace_id
        directory.mkdir(parents=True, exist_ok=True)
        stdout, stderr = directory / "primary.stdout.log", directory / "primary.stderr.log"
        stdout.write_text(process.stdout, encoding="utf-8")
        stderr.write_text(process.stderr, encoding="utf-8")
        status = "unavailable" if process.unavailable else "timed_out" if process.timed_out else "succeeded" if process.return_code == 0 else "failed"
        response = ModelResponse(
            adapter_id="primary-agent", role="Primary Agent", claims=[], final_text=process.stdout,
            execution=CliExecution(
                provider=provider, status=status, command=redact_command(process.command, contains_prompt=True),
                exit_code=process.return_code, started_at=process.started_at, completed_at=process.completed_at,
                duration_ms=process.duration_ms, raw_trace_ref=str(stdout), stderr_ref=str(stderr),
                execution_source_id=process.execution_source_id or source_id, quota_bucket=process.quota_bucket,
                failure_category=process.source_failure or "none",
                requested_model=model, permission="read-only", context_policy=context_policy, error=process.error,
            ),
        )
        store.save_summary(HarnessSummary(task, [response], [], "Primary agent dialogue attempt", "cli_run", trace_id))

    def profiles(self, profiles: dict[str, RoleProfile], project: str) -> dict[str, RoleProfile]:
        """Use one model chain for conversation and all service-owned task decisions.

        Do not rewrite a workspace's roles file. Old standalone CLI loops can still
        replay their controller profile; running services use the primary settings.
        """
        if "controller" not in profiles:
            return dict(profiles)
        chain = self.candidates(project)
        if not chain:
            raise ValueError("主代理尚未設定模型，無法開始或繼續任務")
        original = profiles["controller"]
        from memtrace_harness.execution_sources import split_ref
        candidates = tuple(
            ModelCandidate(split_ref(provider)[0], model, "medium", f"{split_ref(provider)[0]}-account", split_ref(provider)[1])
            for provider, model in chain
        )
        first = candidates[0]
        primary = replace(
            original, role="Primary Agent", provider=first.provider, model=first.model,
            reasoning_effort=first.reasoning_effort, quota_bucket=first.quota_bucket, source=first.source,
            permission="read-only", context_policy="primary-session",
            fallbacks=candidates[1:], max_fallback_hops=len(candidates) - 1,
            fallback_on=("quota_exhausted", "rate_limit", "provider_overloaded"),
        )
        return {**profiles, "controller": primary}

    @staticmethod
    def bind(
        adapters: dict[str, list[ModelAdapter]], *, context: Callable[[], str],
        record: Callable[[str, ModelResponse], None],
    ) -> dict[str, list[ModelAdapter]]:
        return {
            key: [SessionAdapter(adapter, context, record) for adapter in items]
            if key == "controller" else items
            for key, items in adapters.items()
        }


class SessionAdapter(ModelAdapter):
    """Rehydrate the same agent's current discussion at every decision boundary.

    Provider sessions are fresh CLI calls; continuity is explicit, durable context,
    never another skill's private conversation. The delegate preserves raw traces
    and normalizes usage, including unavailable usage, through the existing adapter.
    """

    def __init__(self, delegate: ModelAdapter, context: Callable[[], str], record) -> None:
        self.delegate, self.context, self.record = delegate, context, record

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def run(self, task: TaskEnvelope, trace_id: str) -> ModelResponse:
        current = ContextItem(
            ref="harness:primary-session", title="Primary agent's current project conversation",
            body=self.context(), source="local",
        )
        response = self.delegate.run(replace(
            task, goal=PRIMARY_AGENT_POLICY + "\n" + task.goal,
            context_items=[*[i for i in task.context_items if i.ref != current.ref], current],
        ), trace_id)
        if response.execution.status == "succeeded":
            try:
                artifact = json.loads(response.final_text)
            except ValueError:
                artifact = None
            if isinstance(artifact, dict):
                # Record the explicit decision only, not provider-private reasoning.
                summary = {k: artifact[k] for k in ("action", "reason", "goal_plan", "goal_check", "phases") if k in artifact}
                try:
                    self.record(json.dumps(summary, ensure_ascii=False), response)
                except Exception:
                    # The loop must still persist the authoritative attempt/usage.
                    logger.exception("could not append the primary agent's decision to the discussion")
        return response
