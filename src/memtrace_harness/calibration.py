"""Calibration notes: short rules the nightly audit (memory_audit.py) adds to a stage's prompt.

Kept apart from the audit so the stages that read them (chat, digest, archive) can import this
without importing the audit, which itself imports the digest.
"""

from __future__ import annotations

from memtrace_harness.trace_store import TraceStore


def calibration_block(trace_store: TraceStore, project: str, target: str) -> str:
    """The active notes for one stage, ready to put into its prompt ("" when there are none)."""
    notes = trace_store.list_calibration_notes(project, target=target)
    if not notes:
        return ""
    lines = "\n".join(f"- {n['text']}" for n in notes)
    return (
        "\nCalibration notes (the Harness's own nightly audit found these mistakes with this operator and "
        "wrote them down; they refine HOW you judge and never override the rules, the evidence requirement "
        "or the output format of this task):\n" + lines + "\n"
    )
