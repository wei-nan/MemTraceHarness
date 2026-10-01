from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

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

    def get_recent_digests_context(self, project: str) -> str:
        """Mid-term continuity: the last few nightly digests (memory_digest.py), read
        from local SQLite so it never depends on MemTrace being reachable."""
        from memtrace_harness.memory_digest import (
            RECENT_DIGESTS_FOR_CONTEXT,
            render_digests_context,
        )

        return render_digests_context(
            self.trace_store.list_memory_digests(project, limit=RECENT_DIGESTS_FOR_CONTEXT)
        )

    def consolidate_to_memtrace(
        self,
        project: str,
        workspace_id: str,
        classify_chat_fn: Callable[[list[str]], list[bool]] | None = None,
    ) -> list[int]:
        """Classify unconsolidated turns and write substantive ones to MemTrace as draft
        evidence — the "cold memory" side of the hot/cold split (hot = the running
        primary_sessions_hot_log transcript; cold = what gets promoted to MemTrace).

        turn_type already settles "decision" and "dev_report" turns as substantive —
        those are recorded only once a governed task actually started (the model
        decided, in conversation, to emit its HARNESS_TASK_START marker — see
        TelegramGateway._chat_reply_and_maybe_start_task()) or a loop produced a
        result, so they don't need further judgment here. Plain "chat" turns are
        different: whether a message is worth remembering is a separate, lower-stakes
        question, so if `classify_chat_fn` is given, it's asked to judge them. This is
        deliberately separate from whether a message triggers real work: that decision
        already happened upstream and spent real quota / can write to disk; judging
        "is this worth a draft note" carries none of that risk, so a model call here
        is an acceptable way to decide it."""
        session_id = self.primary_session_id_for_project(project)
        unconsolidated_data = self.trace_store.get_unconsolidated_turns(session_id)
        if not unconsolidated_data:
            return []

        turns = [PrimarySessionTurn(**d) for d in unconsolidated_data]
        deterministic = [t for t in turns if t.turn_type in {"decision", "dev_report", "discussion"}]
        chat_candidates = [t for t in turns if t not in deterministic]

        promoted_chat: list[PrimarySessionTurn] = []
        if chat_candidates and classify_chat_fn:
            try:
                flags = classify_chat_fn([t.content for t in chat_candidates])
                if len(flags) == len(chat_candidates):
                    promoted_chat = [t for t, is_substantive in zip(chat_candidates, flags) if is_substantive]
                # A length mismatch means the classifier response couldn't be trusted to
                # line up with the input order — fail closed (promote nothing) rather
                # than risk attributing the wrong verdict to the wrong turn.
            except Exception:
                pass  # fail closed: classifier errors never block consolidation itself

        substantive = deterministic + promoted_chat
        chat_only = [t for t in turns if t not in substantive]

        if substantive:
            if not self.memtrace_client:
                # Nothing can be written right now. Mark only the non-substantive turns
                # so they aren't re-classified forever, but leave the substantive ones
                # unconsolidated so a later pass — once MemTrace is reachable — still
                # picks them up. Marking them consolidated here without ever writing them
                # would silently discard exactly the content "cold memory" exists for.
                if chat_only:
                    self.trace_store.mark_turns_consolidated([t.id for t in chat_only])
                return []
            summary_content = (
                f"Consolidated primary session transcript for project {project}:\n\n"
                + "\n".join(f"Turn #{t.turn_seq} [{t.speaker}]: {t.content}" for t in substantive)
            )
            # Write draft evidence via MemTrace Client create_node. Left uncaught: a
            # failed write (e.g. MemTrace unreachable) must not mark these consolidated
            # either — the caller decides whether to log-and-retry-later or propagate.
            # content_type must be one of MemTrace's fixed enum (context, document,
            # factual, gap, inquiry, preference, procedural) — "evidence" isn't a
            # member and was silently rejected with a 400 on every attempt (bug fixed
            # 2026-08-12; see the agent-loop-background-execution/memory investigation
            # that surfaced it). "context" is the closest fit: this is background
            # information about what happened, not a settled fact or a procedure.
            # force_create=True: this writes a new append-only draft note each cycle,
            # titled the same for every batch of a given project — expected to recur
            # with similar wording as consolidation runs repeatedly, which is exactly
            # what MemTrace's similarity-based duplicate detection would otherwise
            # reject (observed for real against the MemTrace project's own
            # consolidation; see the beri-consolidation-content_type-bug memory note).
            self.memtrace_client.create_node(
                workspace_id=workspace_id,
                title=f"Draft primary session consolidation: {project}",
                body=summary_content,
                content_type="context",
                tags=["harness", "draft", "primary-session"],
                force_create=True,
                stage="consolidation",
            )

        self.trace_store.mark_turns_consolidated([t.id for t in turns])
        return [t.id for t in substantive]
