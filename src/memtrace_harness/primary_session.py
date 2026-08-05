from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.trace_store import TraceStore
    from memtrace_harness.runner import HarnessRunner


@dataclass
class PrimarySessionTurn:
    id: int
    primary_session_id: str
    project: str
    turn_seq: int
    created_at: str
    speaker: str
    provider: str | None
    model: str | None
    turn_type: str
    content: str
    source_work_conversation_id: str | None
    consolidated: bool


class PrimarySessionManager:
    def __init__(self, trace_store: TraceStore, memtrace_client: MemTraceClient | None = None) -> None:
        self.trace_store = trace_store
        self.memtrace_client = memtrace_client

    def primary_session_id_for_project(self, project_name: str) -> str:
        return f"psess_{project_name}"

    def record_turn(
        self,
        *,
        project: str,
        speaker: str,
        turn_type: str,
        content: str,
        provider: str | None = None,
        model: str | None = None,
        source_work_conversation_id: str | None = None,
    ) -> int:
        session_id = self.primary_session_id_for_project(project)
        return self.trace_store.append_primary_session_turn(
            primary_session_id=session_id,
            project=project,
            speaker=speaker,
            turn_type=turn_type,
            content=content,
            provider=provider,
            model=model,
            source_work_conversation_id=source_work_conversation_id,
        )

    def get_rehydration_context(
        self, project: str, recent_window_size: int = 10
    ) -> str:
        session_id = self.primary_session_id_for_project(project)
        turns_data = self.trace_store.get_primary_session_turns(session_id)
        if not turns_data:
            return ""

        turns = [PrimarySessionTurn(**d) for d in turns_data]
        if len(turns) <= recent_window_size:
            recent_turns = turns
            older_summary = ""
        else:
            recent_turns = turns[-recent_window_size:]
            older_turns = turns[:-recent_window_size]
            substantive_older = [t for t in older_turns if t.turn_type in {"decision", "dev_report", "discussion"}]
            older_summary = "Earlier substantive context:\n" + "\n".join(
                f"- [{t.speaker}]: {t.content[:150]}" for t in substantive_older[-5:]
            )

        recent_formatted = "\n".join(
            f"[{t.speaker}]: {t.content}" for t in recent_turns
        )
        if older_summary:
            return f"{older_summary}\n\nRecent transcript:\n{recent_formatted}"
        return f"Recent transcript:\n{recent_formatted}"

    def consolidate_to_memtrace(
        self, project: str, workspace_id: str
    ) -> list[int]:
        """Classify unconsolidated turns and write substantive ones to MemTrace as draft evidence."""
        session_id = self.primary_session_id_for_project(project)
        unconsolidated_data = self.trace_store.get_unconsolidated_turns(session_id)
        if not unconsolidated_data:
            return []

        substantive_turn_ids: list[int] = []
        substantive_contents: list[str] = []

        for d in unconsolidated_data:
            turn = PrimarySessionTurn(**d)
            # Substantive turns are decision, dev_report, or discussion
            is_substantive = turn.turn_type in {"decision", "dev_report", "discussion"}

            if is_substantive:
                substantive_turn_ids.append(turn.id)
                substantive_contents.append(f"Turn #{turn.turn_seq} [{turn.speaker}]: {turn.content}")

        all_turn_ids = [d["id"] for d in unconsolidated_data]

        if substantive_contents and self.memtrace_client:
            summary_content = (
                f"Consolidated primary session transcript for project {project}:\n\n"
                + "\n".join(substantive_contents)
            )
            # Write draft evidence via MemTrace Client create_node
            self.memtrace_client.create_node(
                workspace_id=workspace_id,
                title=f"Draft primary session consolidation: {project}",
                body=summary_content,
                content_type="evidence",
                tags=["harness", "draft", "primary-session"],
            )

        # Mark all processed turns as consolidated
        self.trace_store.mark_turns_consolidated(all_turn_ids)
        return substantive_turn_ids
