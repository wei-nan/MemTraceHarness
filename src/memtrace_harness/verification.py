"""Reading the result of a project's ``verify_command``: did it pass, could it not run, or did it fail — and
if it failed, is the failure the task's or one the base checkout already has?

The Harness runs the command itself, so the result is ground truth, but only a failure the task caused is a
reason to send work back. A missing tool or SDK, and a test that already fails on the base branch, are
information for the operator and the Developer; they are not the Developer's to fix and not a gate."""

from __future__ import annotations

import re
from typing import Any

PASSED = "passed"
COULD_NOT_RUN = "could_not_run"
BASELINE_FAILING = "baseline_failing"
REGRESSION = "regression"

# Only a REGRESSION sends work back. The others are reported and let the work go on.
BLOCKING = frozenset({REGRESSION})

# Output that says the command could not do its job here, as opposed to the code under test failing.
_COULD_NOT_RUN = (
    re.compile(r"^MISSING_TOOL:", re.MULTILINE),
    re.compile(r"command not found"),
    re.compile(r"SDK location not found"),
)

# One normalised line per failed test, for the common runners (Gradle/JUnit, XCTest, TAP, pytest, go test).
_FAILED_TEST = (
    re.compile(r"^\s*(\S.* > .+) FAILED\s*$", re.MULTILINE),
    re.compile(r"Test Case '(.+?)' failed", re.MULTILINE),
    re.compile(r"^\s*not ok \d+ - (.+?)\s*$", re.MULTILINE),
    re.compile(r"^FAILED (\S+)", re.MULTILINE),
    re.compile(r"^--- FAIL: (\S+)", re.MULTILINE),
)
_TIMING = re.compile(r"\s*\(\d+(\.\d+)? ?(seconds|s|ms)\)")


def failed_tests(text: str) -> set[str]:
    """The names of the tests a runner's output says failed (empty when the output names none)."""
    found: set[str] = set()
    for pattern in _FAILED_TEST:
        for match in pattern.finditer(text or ""):
            found.add(_TIMING.sub("", match.group(1)).strip())
    return found


def _output(result: dict[str, Any]) -> str:
    return f"{result.get('stdout_tail') or ''}\n{result.get('stderr_tail') or ''}"


def could_not_run(result: dict[str, Any]) -> bool:
    return bool(result.get("timed_out")) or result.get("exit_code") == 127 or any(
        pattern.search(_output(result)) for pattern in _COULD_NOT_RUN
    )


def classify(result: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
    """``result`` with ``status`` (passed / could_not_run / baseline_failing / regression), ``new_failures`` and
    ``baseline_failures`` added. ``baseline`` is the same command run on the base checkout, or None when there
    is nothing to compare with (the task works in the main checkout itself); a failure that cannot be told
    apart from the base's is then a regression, the safe reading."""
    out = dict(result)
    if result.get("passed"):
        out.update(status=PASSED, new_failures=[], baseline_failures=[])
        return out
    if could_not_run(result):
        out.update(status=COULD_NOT_RUN, new_failures=[], baseline_failures=[])
        return out
    mine = failed_tests(_output(result))
    if baseline is None or baseline.get("passed") or could_not_run(baseline):
        out.update(status=REGRESSION, new_failures=sorted(mine), baseline_failures=[])
        return out
    base = failed_tests(_output(baseline))
    new = mine - base
    # Unnamed failures (no recognisable test line) cannot be shown to be the base's, so they stay blocking.
    out.update(
        status=BASELINE_FAILING if mine and not new else REGRESSION,
        new_failures=sorted(new),
        baseline_failures=sorted(base),
    )
    return out


def describe(result: dict[str, Any]) -> str:
    """One paragraph for the Developer, the Controller and the operator."""
    status = result.get("status")
    command = result.get("command")
    if status == PASSED:
        return f"Verification passed ({command})."
    if status == COULD_NOT_RUN:
        return (
            f"Verification could not run here ({command}); this is the environment, not the change. "
            f"Last output:\n{(result.get('stderr_tail') or result.get('stdout_tail') or '')[-1500:]}"
        )
    if status == BASELINE_FAILING:
        return (
            f"Verification fails, but only on tests that already fail on the base checkout "
            f"({', '.join(result.get('baseline_failures') or []) or 'see output'}). The change adds no new failure."
        )
    new = result.get("new_failures") or []
    return (
        f"Verification failed ({command}); exit {result.get('exit_code')}"
        + (f"; new failures: {', '.join(new)}" if new else "")
        + f".\n{(result.get('stderr_tail') or result.get('stdout_tail') or '')[-2500:]}"
    )
