from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Literal, Mapping, cast

from memtrace_harness.fallback import CROSS_PROVIDER_FALLBACK_CATEGORIES
from memtrace_harness.schemas import FailureCategory, ProviderId


Permission = Literal["read-only", "workspace-write"]
ContextPolicy = Literal[
    "loop-snapshot",
    "task-and-targeted-evidence",
    "gate-evidence-only",
    "accepted-plan-and-repo",
]

REQUIRED_PROFILES = (
    "controller",
    "planner",
    "planner-escalation",
    "red-team",
    "developer",
)


@dataclass(frozen=True)
class ModelCandidate:
    provider: ProviderId
    model: str
    reasoning_effort: str
    quota_bucket: str

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, object],
        *,
        label: str,
    ) -> "ModelCandidate":
        provider = str(data.get("provider", ""))
        model = str(data.get("model", ""))
        reasoning_effort = str(data.get("reasoning_effort", ""))
        if not provider or not model or not reasoning_effort:
            raise ValueError(f"Model candidate {label!r} is incomplete")
        _validate_provider_effort(label, provider, reasoning_effort)
        quota_bucket = str(data.get("quota_bucket") or f"{provider}-account")
        if not quota_bucket.strip():
            raise ValueError(f"Model candidate {label!r} has an empty quota_bucket")
        return cls(
            provider=cast(ProviderId, provider),
            model=model,
            reasoning_effort=reasoning_effort,
            quota_bucket=quota_bucket,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "quota_bucket": self.quota_bucket,
        }


@dataclass(frozen=True)
class RoleProfile:
    profile_id: str
    role: str
    provider: ProviderId
    model: str
    reasoning_effort: str
    permission: Permission
    context_policy: ContextPolicy
    max_attempts: int = 1
    quota_bucket: str = ""
    fallbacks: tuple[ModelCandidate, ...] = ()
    fallback_on: tuple[FailureCategory, ...] = ()
    max_fallback_hops: int = 0
    fallback_cooldown_seconds: int = 1800

    @classmethod
    def from_mapping(cls, profile_id: str, data: Mapping[str, object]) -> "RoleProfile":
        required = (
            "role",
            "provider",
            "model",
            "reasoning_effort",
            "permission",
            "context_policy",
        )
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(
                f"Role profile {profile_id!r} is missing: {', '.join(missing)}"
            )
        provider = str(data["provider"])
        permission = str(data["permission"])
        if permission not in {"read-only", "workspace-write"}:
            raise ValueError(
                f"Role profile {profile_id!r} has invalid permission: {permission}"
            )
        context_policy = str(data["context_policy"])
        allowed_context = {
            "loop-snapshot",
            "task-and-targeted-evidence",
            "gate-evidence-only",
            "accepted-plan-and-repo",
        }
        if context_policy not in allowed_context:
            raise ValueError(
                f"Role profile {profile_id!r} has invalid context policy: {context_policy}"
            )
        max_attempts = int(data.get("max_attempts", 1))
        if max_attempts != 1:
            raise ValueError(
                f"Role profile {profile_id!r} must use max_attempts=1; "
                "retries are routed explicitly"
            )
        reasoning_effort = str(data["reasoning_effort"])
        _validate_provider_effort(profile_id, provider, reasoning_effort)
        raw_fallbacks = data.get("fallbacks", [])
        if not isinstance(raw_fallbacks, list):
            raise ValueError(f"Role profile {profile_id!r} fallbacks must be an array")
        fallbacks = tuple(
            ModelCandidate.from_mapping(item, label=f"{profile_id}.fallbacks[{index}]")
            for index, item in enumerate(raw_fallbacks)
            if isinstance(item, dict)
        )
        if len(fallbacks) != len(raw_fallbacks):
            raise ValueError(f"Role profile {profile_id!r} has an invalid fallback entry")
        raw_fallback_on = data.get("fallback_on", [])
        if not isinstance(raw_fallback_on, list) or not all(
            isinstance(item, str) for item in raw_fallback_on
        ):
            raise ValueError(f"Role profile {profile_id!r} fallback_on must be a string array")
        fallback_on = tuple(cast(FailureCategory, item) for item in raw_fallback_on)
        invalid_categories = sorted(
            set(fallback_on).difference(CROSS_PROVIDER_FALLBACK_CATEGORIES)
        )
        if invalid_categories:
            raise ValueError(
                f"Role profile {profile_id!r} has unsafe fallback categories: "
                f"{', '.join(invalid_categories)}"
            )
        max_fallback_hops = int(data.get("max_fallback_hops", len(fallbacks)))
        if max_fallback_hops < 0 or max_fallback_hops > len(fallbacks):
            raise ValueError(
                f"Role profile {profile_id!r} max_fallback_hops exceeds its candidates"
            )
        cooldown = int(data.get("fallback_cooldown_seconds", 1800))
        if cooldown <= 0:
            raise ValueError(
                f"Role profile {profile_id!r} fallback_cooldown_seconds must be positive"
            )
        if fallbacks and not fallback_on:
            raise ValueError(
                f"Role profile {profile_id!r} has fallbacks but no eligible failure categories"
            )
        return cls(
            profile_id=profile_id,
            role=str(data["role"]),
            provider=cast(ProviderId, provider),
            model=str(data["model"]),
            reasoning_effort=reasoning_effort,
            permission=cast(Permission, permission),
            context_policy=cast(ContextPolicy, context_policy),
            max_attempts=max_attempts,
            quota_bucket=str(data.get("quota_bucket") or f"{provider}-account"),
            fallbacks=fallbacks,
            fallback_on=fallback_on,
            max_fallback_hops=max_fallback_hops,
            fallback_cooldown_seconds=cooldown,
        )

    @property
    def candidates(self) -> tuple[ModelCandidate, ...]:
        primary = ModelCandidate(
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            quota_bucket=self.quota_bucket or f"{self.provider}-account",
        )
        return (primary, *self.fallbacks[: self.max_fallback_hops])

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "permission": self.permission,
            "context_policy": self.context_policy,
            "max_attempts": self.max_attempts,
            "quota_bucket": self.quota_bucket,
            "fallbacks": [item.to_dict() for item in self.fallbacks],
            "fallback_on": list(self.fallback_on),
            "max_fallback_hops": self.max_fallback_hops,
            "fallback_cooldown_seconds": self.fallback_cooldown_seconds,
        }


def load_role_profiles(path: Path | None = None) -> dict[str, RoleProfile]:
    source = path or Path(__file__).with_name("default-role-profiles.toml")
    with source.open("rb") as handle:
        document = tomllib.load(handle)
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"Role profile file has no [profiles] table: {source}")
    profiles = {
        str(profile_id): RoleProfile.from_mapping(str(profile_id), data)
        for profile_id, data in raw_profiles.items()
        if isinstance(data, dict)
    }
    missing = [profile_id for profile_id in REQUIRED_PROFILES if profile_id not in profiles]
    if missing:
        raise ValueError(f"Role profile file is missing required profiles: {', '.join(missing)}")
    _validate_role_boundaries(profiles)
    return profiles


def _validate_role_boundaries(profiles: Mapping[str, RoleProfile]) -> None:
    """Provider/model choice is fully project-configurable for every role — Controller,
    Planner, Planner-escalation, Red Team, and Developer alike. A role's identity is its
    job in the pipeline (route, plan, escalate, verify, implement), not a vendor pin; the
    "independent reviewer" and "consistent behavior" properties people actually rely on
    come from each stage being a fresh, separately-invoked CLI call and from the
    permission/context boundaries below, not from forcing a specific model onto a role.

    What stays enforced, because these are safety/architecture properties independent of
    which vendor executes a role:
    - each role's permission level (only Developer may hold workspace-write — every
      other role must stay read-only);
    - each role's context_policy (how much of the repo/evidence it is allowed to see —
      e.g. Controller only ever gets a loop snapshot, Red Team only gate-scoped
      evidence, regardless of which model is behind it);
    - Red Team and Planner-escalation both fail closed with no fallback chain, so a gate
      verdict or an escalated plan never silently degrades to a weaker/different model
      mid-stage without a human noticing."""
    expected_shape = {
        "controller": ("read-only", "loop-snapshot"),
        "planner": ("read-only", "task-and-targeted-evidence"),
        "planner-escalation": ("read-only", "task-and-targeted-evidence"),
        "red-team": ("read-only", "gate-evidence-only"),
        "developer": ("workspace-write", "accepted-plan-and-repo"),
    }
    for profile_id, (permission, context_policy) in expected_shape.items():
        profile = profiles[profile_id]
        if profile.permission != permission or profile.context_policy != context_policy:
            raise ValueError(
                f"Role profile {profile_id!r} must remain {permission}/{context_policy}"
            )
    writable = [
        item.profile_id
        for item in profiles.values()
        if item.permission == "workspace-write"
    ]
    if writable != ["developer"]:
        raise ValueError("Only the developer role profile may use workspace-write")
    if profiles["red-team"].fallbacks:
        raise ValueError("Red Team must fail closed unless a future policy explicitly changes it")
    if profiles["planner-escalation"].fallbacks:
        raise ValueError("Planning escalation must not silently downgrade to a different model")


def _validate_provider_effort(label: str, provider: str, reasoning_effort: str) -> None:
    allowed_effort = {
        "antigravity": {"low", "medium", "high"},
        "claude": {"low", "medium", "high", "xhigh", "max"},
        "codex": {"minimal", "low", "medium", "high", "xhigh"},
    }
    if provider not in allowed_effort:
        raise ValueError(f"Model candidate {label!r} has unknown provider: {provider}")
    if reasoning_effort not in allowed_effort[provider]:
        raise ValueError(
            f"Model candidate {label!r} has invalid {provider} effort: {reasoning_effort}"
        )
