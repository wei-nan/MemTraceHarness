from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import tomllib
from typing import Literal, Mapping, cast

from memtrace_harness.fallback import CROSS_PROVIDER_FALLBACK_CATEGORIES
from memtrace_harness.schemas import FailureCategory, ProviderId

logger = logging.getLogger(__name__)

Permission = Literal["read-only", "workspace-write"]
ContextPolicy = Literal[
    "primary-session",
    "loop-snapshot",
    "task-and-targeted-evidence",
    "gate-evidence-only",
    "accepted-plan-and-repo",
]

# The Controller is the only role. Everything else is a stage skill it runs (stage_skills.py):
# `plan`, `review` (the G1/G2 gates) and `develop`, plus `plan-escalation`, the plan skill's
# stronger-model mode. Profile ids are those skill names; the pre-skill role names still load.
REQUIRED_PROFILES = (
    "controller",
    "plan",
    "plan-escalation",
    "review",
    "develop",
)
LEGACY_PROFILE_IDS = {
    "planner": "plan",
    "planner-escalation": "plan-escalation",
    "red-team": "review",
    "developer": "develop",
}
# a profile id as it may still appear in older rows, files and chat requests -> the id that
# stands for it now (canonical ids map to themselves)
def canonical_profile_id(profile_id: str) -> str:
    return LEGACY_PROFILE_IDS.get(profile_id, profile_id)


def profile_id_variants(profile_id: str) -> tuple[str, ...]:
    """The canonical id and every legacy spelling of it (for matching stored rows and file headers)."""
    canonical = canonical_profile_id(profile_id)
    return (canonical, *(old for old, new in LEGACY_PROFILE_IDS.items() if new == canonical))


@dataclass(frozen=True)
class ModelCandidate:
    provider: ProviderId
    model: str
    reasoning_effort: str
    quota_bucket: str
    source: str | None = None

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
            source=_source(data.get("source")),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "quota_bucket": self.quota_bucket,
            "source": self.source,
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
    # Per-role CLI wall-clock limit; None means use the global
    # HARNESS_CLI_TIMEOUT_SECONDS. Long-running roles (Developer running backtests)
    # need more than the short routing/review stages should be allowed to hang for.
    timeout_seconds: int | None = None
    source: str | None = None

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
            "primary-session",
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
        raw_timeout = data.get("timeout_seconds")
        timeout_seconds = int(raw_timeout) if raw_timeout is not None else None
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError(f"Role profile {profile_id!r} timeout_seconds must be positive")
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
            timeout_seconds=timeout_seconds,
            source=_source(data.get("source")),
        )

    @property
    def candidates(self) -> tuple[ModelCandidate, ...]:
        primary = ModelCandidate(
            provider=self.provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            quota_bucket=self.quota_bucket or f"{self.provider}-account",
            source=self.source,
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
            "timeout_seconds": self.timeout_seconds,
            "source": self.source,
        }


def load_role_profiles(path: Path | None = None) -> dict[str, RoleProfile]:
    source = path or Path(__file__).with_name("default-role-profiles.toml")
    with source.open("rb") as handle:
        document = tomllib.load(handle)
    raw_profiles = document.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"Role profile file has no [profiles] table: {source}")
    profiles = {
        profile_id: RoleProfile.from_mapping(profile_id, data)
        for profile_id, data in _normalized_profile_tables(raw_profiles).items()
    }
    missing = [profile_id for profile_id in REQUIRED_PROFILES if profile_id not in profiles]
    if missing:
        raise ValueError(f"Role profile file is missing required profiles: {', '.join(missing)}")
    _validate_role_boundaries(profiles)
    return profiles


def _normalized_profile_tables(raw_profiles: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """The profile tables under their skill names: a legacy key (planner, red-team, developer,
    planner-escalation) is read as its skill, and the plan skill's `[profiles.plan.escalation]`
    sub-table becomes the `plan-escalation` profile, which inherits the plan's permission and
    context policy. A file that spells the same profile both ways is ambiguous and refused."""
    tables: dict[str, Mapping[str, object]] = {}
    for key, data in raw_profiles.items():
        if not isinstance(data, dict):
            continue
        profile_id = canonical_profile_id(str(key))
        if profile_id in tables:
            raise ValueError(f"Role profile {profile_id!r} is defined twice (also as a legacy name)")
        tables[profile_id] = data
    plan = tables.get("plan")
    nested = plan.get("escalation") if isinstance(plan, dict) else None
    if isinstance(nested, dict):
        if "plan-escalation" in tables:
            raise ValueError("Role profile 'plan-escalation' is defined twice (also as [profiles.plan.escalation])")
        assert isinstance(plan, dict)
        tables["plan-escalation"] = {
            "role": "Plan Escalation",
            "permission": plan.get("permission"),
            "context_policy": plan.get("context_policy"),
            **nested,
        }
        tables["plan"] = {key: value for key, value in plan.items() if key != "escalation"}
    return tables


def _validate_role_boundaries(profiles: Mapping[str, RoleProfile]) -> None:
    """Provider/model choice is fully project-configurable for every profile: the Controller and
    the plan, plan-escalation, review and develop skills alike. A skill's identity is its job
    (plan, escalate, verify, implement), not a vendor pin. The "independent reviewer" property
    comes from every skill running as a fresh, separately-invoked CLI call that is handed
    artifacts, never another skill's conversation (so `review` never sees `develop`'s
    transcript), and from the permission/context boundaries below. Pointing `review` at the same
    provider/model as `develop` is allowed but logged, since it weakens that independence in
    practice even though the context stays separate.

    What stays enforced, because these are safety/architecture properties independent of
    which vendor executes a skill:
    - each profile's permission level (only `develop` may hold workspace-write; the Controller
      and every other skill stay read-only);
    - each profile's context_policy (how much of the repo/evidence it is allowed to see:
      the primary agent gets conversation plus snapshot, legacy controllers a snapshot, and review gate-scoped evidence,
      regardless of which model is behind it).

    `review` and `plan-escalation` may carry a fallback chain too. The original concern was a
    gate verdict or an escalated plan silently degrading to a weaker/different model, but every
    fallback is recorded on the stage result (`fallback_from_model`) and surfaced in the
    human-facing completion summary, so it is a visible resilience trade, not a stealth downgrade."""
    expected_shape = {
        "controller": ("read-only", "loop-snapshot"),
        "plan": ("read-only", "task-and-targeted-evidence"),
        "plan-escalation": ("read-only", "task-and-targeted-evidence"),
        "review": ("read-only", "gate-evidence-only"),
        "develop": ("workspace-write", "accepted-plan-and-repo"),
    }
    for profile_id, (permission, context_policy) in expected_shape.items():
        profile = profiles[profile_id]
        allowed_contexts = {context_policy}
        if profile_id == "controller":
            allowed_contexts.add("primary-session")
        if profile.permission != permission or profile.context_policy not in allowed_contexts:
            raise ValueError(
                f"Role profile {profile_id!r} must remain {permission}/{context_policy}"
            )
    writable = [
        item.profile_id
        for item in profiles.values()
        if item.permission == "workspace-write"
    ]
    if writable != ["develop"]:
        raise ValueError("Only the develop skill profile may use workspace-write")
    review, develop = profiles["review"], profiles["develop"]
    if (review.provider, review.model) == (develop.provider, develop.model):
        logger.warning(
            "the review skill and the develop skill both use %s/%s: review still runs in a fresh "
            "context, but a different provider or model would make it a more independent check",
            review.provider,
            review.model,
        )


def _validate_provider_effort(label: str, provider: str, reasoning_effort: str) -> None:
    allowed_effort = {
        "antigravity": {"low", "medium", "high"},
        "claude": {"low", "medium", "high", "xhigh", "max"},
        "codex": {"minimal", "low", "medium", "high", "xhigh"},
        # Cursor puts the tier in the model name; the value is recorded, never passed to the CLI.
        "cursor": {"low", "medium", "high", "xhigh", "max"},
    }
    if provider not in allowed_effort:
        raise ValueError(f"Model candidate {label!r} has unknown provider: {provider}")
    if reasoning_effort not in allowed_effort[provider]:
        raise ValueError(
            f"Model candidate {label!r} has invalid {provider} effort: {reasoning_effort}"
        )


def _source(value) -> str | None:
    if value in (None, ""):
        return None
    from memtrace_harness.execution_sources import ID
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("invalid execution source id")
    return value
