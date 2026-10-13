"""A model chosen for one task only ("用 opus 5.5 來設計這個頁面").

The chat model writes `model=<model>::` (or `model=<provider>/<model>::`) in front of a task goal.
That model becomes the Developer's first candidate for this task; the project's own Developer
chain follows it, so a model that cannot be reached or that fails falls back to the usual default
(`AgentLoopRunner(model_override_profiles=...)` lifts the capacity-only fallback limit for it).
Nothing is written to models.toml or the role-profiles file: the next task is back to the defaults.
"""

from __future__ import annotations

from dataclasses import replace

from memtrace_harness.config import HarnessConfig
from memtrace_harness.role_profiles import ModelCandidate, RoleProfile, _validate_provider_effort

OVERRIDE_PROFILE = "develop"
PROVIDERS = ("claude", "codex", "antigravity")
_CLAUDE_ALIASES = {"opus", "sonnet", "haiku"}


def infer_provider(model: str, config: HarnessConfig | None = None) -> str | None:
    if model in _CLAUDE_ALIASES or model.startswith("claude-"):
        return "claude"
    if model.startswith("gpt-"):
        return "codex"
    if model.startswith("gemini"):
        return "antigravity"
    if config is None:
        return None
    from memtrace_harness.model_catalog import extra_models, provider_models

    known = provider_models(config)
    extra = extra_models(config.workspace_root)
    for provider in PROVIDERS:
        if model in known.get(provider, []) or model in extra.get(provider, []):
            return provider
    return None


def resolve_model_override(spec: str, developer: RoleProfile, config: HarnessConfig) -> ModelCandidate:
    """The candidate a `model=` spec names. Raises ValueError (with text for the operator)
    when the provider cannot be told."""
    provider, _, model = spec.rpartition("/")
    provider = provider or (infer_provider(model, config) or "")
    if provider not in PROVIDERS:
        raise ValueError(
            f"認不出模型「{spec}」屬於哪個供應商；請寫成 claude/<模型>、codex/<模型> 或 antigravity/<模型>。"
        )
    effort = developer.reasoning_effort
    try:
        _validate_provider_effort(OVERRIDE_PROFILE, provider, effort)
    except ValueError:
        effort = "high"
    return ModelCandidate(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        reasoning_effort=effort,
        quota_bucket=f"{provider}-account",
    )


def with_developer_override(
    profiles: dict[str, RoleProfile], candidate: ModelCandidate
) -> dict[str, RoleProfile]:
    """`profiles` with the Developer trying `candidate` first and its own chain after it."""
    developer = profiles[OVERRIDE_PROFILE]
    chain = (
        ModelCandidate(
            provider=developer.provider,
            model=developer.model,
            reasoning_effort=developer.reasoning_effort,
            quota_bucket=developer.quota_bucket or f"{developer.provider}-account",
        ),
        *developer.fallbacks[: developer.max_fallback_hops],
    )
    overridden = replace(
        developer,
        provider=candidate.provider,
        model=candidate.model,
        reasoning_effort=candidate.reasoning_effort,
        quota_bucket=candidate.quota_bucket,
        fallbacks=chain,
        fallback_on=developer.fallback_on or ("quota_exhausted", "rate_limit", "provider_overloaded"),
        max_fallback_hops=len(chain),
    )
    return {**profiles, OVERRIDE_PROFILE: overridden}


def describe(candidate: ModelCandidate) -> str:
    return f"{candidate.provider}/{candidate.model}"
