from unittest import TestCase

from memtrace_harness.fallback import (
    classify_execution_failure,
    permits_cross_provider_fallback,
)
from memtrace_harness.schemas import CliExecution


def execution(*, status: str = "failed", error: str | None = None) -> CliExecution:
    return CliExecution(
        provider="codex",
        status=status,
        command=["codex"],
        exit_code=1,
        started_at="2026-08-04T00:00:00+00:00",
        completed_at="2026-08-04T00:00:01+00:00",
        duration_ms=1000,
        error=error,
    )


class FailureClassifierTests(TestCase):
    def test_only_capacity_failures_allow_cross_provider_fallback(self) -> None:
        cases = {
            "Quota exhausted for this weekly limit": "quota_exhausted",
            "429 too many requests": "rate_limit",
            "Provider overloaded; try later": "provider_overloaded",
            "maximum context length exceeded": "context_limit",
            "Access denied": "permission",
            "login required": "authentication",
            "connection refused": "network",
            "unrecognized provider failure": "unknown",
        }

        for message, expected in cases.items():
            with self.subTest(message=message):
                category = classify_execution_failure(execution(error=message))
                self.assertEqual(category, expected)
                self.assertEqual(
                    permits_cross_provider_fallback(category),
                    expected in {"quota_exhausted", "rate_limit", "provider_overloaded"},
                )

    def test_timeout_is_not_a_model_fallback_signal(self) -> None:
        category = classify_execution_failure(
            execution(status="timed_out", error="CLI timed out")
        )

        self.assertEqual(category, "timeout")
        self.assertFalse(permits_cross_provider_fallback(category))
