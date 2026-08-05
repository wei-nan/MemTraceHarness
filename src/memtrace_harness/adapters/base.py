from __future__ import annotations

from abc import ABC, abstractmethod

from memtrace_harness.schemas import ModelResponse, TaskEnvelope


class ModelAdapter(ABC):
    adapter_id: str
    role: str

    @abstractmethod
    def run(self, task: TaskEnvelope, trace_id: str) -> ModelResponse:
        """Run the adapter against a normalized task envelope."""
