from __future__ import annotations

from hashlib import sha256

from memtrace_harness.schemas import CliExecution, FailureCategory


CROSS_PROVIDER_FALLBACK_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {"quota_exhausted", "rate_limit", "provider_overloaded"}
)


def classify_execution_failure(execution: CliExecution) -> FailureCategory:
    if execution.status == "succeeded":
        return "none"
    if execution.status == "timed_out":
        return "timeout"

    detail = " ".join(
        item
        for item in [execution.error, *execution.parse_warnings]
        if isinstance(item, str) and item.strip()
    ).lower()

    if _contains(
        detail,
        "quota exhausted",
        "quota reached",
        "usage limit",
        "weekly limit",
        "5-hour limit",
    ):
        return "quota_exhausted"
    if _contains(detail, "rate limit", "too many requests", "429"):
        return "rate_limit"
    if _contains(
        detail,
        "overloaded",
        "capacity",
        "temporarily unavailable",
        "currently unavailable",
        "service unavailable",
        "code 503",
        "eligibility check failed",
        "timeout waiting for response",
    ):
        return "provider_overloaded"
    if _contains(detail, "context window", "context length", "maximum context", "too many tokens"):
        return "context_limit"
    if _contains(detail, "authentication", "unauthorized", "invalid token", "login required"):
        return "authentication"
    if _contains(detail, "access denied", "permission denied", "operation not permitted"):
        return "permission"
    if _contains(detail, "not found", "no such file", "is not recognized", "cannot find"):
        return "configuration"
    if _contains(detail, "connection reset", "connection refused", "dns", "network"):
        return "network"
    if _contains(detail, "json schema", "schema validation", "invalid structured output"):
        return "schema"
    if _contains(
        detail,
        "safety policy",
        "policy refusal",
        "content policy",
        "safety violation",
        "modified the working directory despite permission=read-only",
    ):
        return "safety"
    return "unknown"


def permits_cross_provider_fallback(category: FailureCategory) -> bool:
    return category in CROSS_PROVIDER_FALLBACK_CATEGORIES


def error_signature(execution: CliExecution) -> str | None:
    detail = (execution.error or "").strip().lower()
    if not detail:
        return None
    return sha256(detail.encode("utf-8")).hexdigest()[:16]


def _contains(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)
