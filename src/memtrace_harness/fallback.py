from __future__ import annotations

from hashlib import sha256

from memtrace_harness.schemas import CliExecution, FailureCategory


CROSS_PROVIDER_FALLBACK_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {"quota_exhausted", "rate_limit", "provider_overloaded", "cli_unavailable", "authentication"}
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
    if _contains(
        detail,
        "authentication",
        "failed to authenticate",
        "unauthorized",
        "invalid token",
        "login required",
        "oauth session expired",
        "please run /login",
    ):
        return "authentication"
    if _contains(detail, "access denied", "permission denied", "operation not permitted"):
        return "permission"
    # The provider CLI itself could not be launched (binary moved/uninstalled, a wrapper
    # script pointing at a stale path). Nothing about the request was ever evaluated, so
    # trying the next provider is safe — unlike a generic "configuration" failure.
    if _contains(
        detail,
        "cli executable not found",
        "cannot execute",
        "exec format error",
        "exec: ",
    ) and _contains(detail, "not found", "no such file", "cannot execute", "exec format error"):
        return "cli_unavailable"
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


def permits_read_only_timeout_fallback(category: FailureCategory, permission: str) -> bool:
    """A timed-out call of a read-only profile (the Controller, plan, review) may go to the next candidate.
    It cannot have changed anything, and a call that hangs for the whole timeout says nothing about the
    request itself (2026-10-13, TWTradingStrategy chat_c8eddfd8: a Codex Controller call that normally takes
    7-15 seconds sat silent for 900 and failed the whole task). A write-capable profile is excluded: its
    timed-out call may already have edited files, so another model is not simply tried in its place."""
    return category == "timeout" and permission == "read-only"


def error_signature(execution: CliExecution) -> str | None:
    detail = (execution.error or "").strip().lower()
    if not detail:
        return None
    return sha256(detail.encode("utf-8")).hexdigest()[:16]


def _contains(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)
