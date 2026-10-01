from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Callable

from memtrace_harness.memory_drafts import draft_title

if TYPE_CHECKING:
    from memtrace_harness.memtrace_client import MemTraceClient
    from memtrace_harness.trace_store import TraceStore
    from memtrace_harness.runner import HarnessRunner


# A scheduled run is logged as a pair of turns: schedule_trigger when it fires,
# schedule_report with its result. They carry the schedule's id and are kept OUT of the
# conversation window, the "earlier substantive context" slots and the hourly MemTrace
# archive: a 10-minute schedule used to make up over half of one project's log, and at
# one real moment 8 of the 10 turns the chat model saw were schedule noise. Instead the
# model gets a separate block with the latest result per schedule (see
# get_rehydration_context()).
SCHEDULE_TRIGGER = "schedule_trigger"
SCHEDULE_REPORT = "schedule_report"
SCHEDULE_TURN_TYPES = frozenset({SCHEDULE_TRIGGER, SCHEDULE_REPORT})
# Only schedules active within this long are mentioned in the context.
SCHEDULED_RUNS_LOOKBACK = timedelta(hours=24)
SCHEDULED_RUNS_MAX = 6
SCHEDULED_RESULT_CHARS = 500


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
    schedule_id: str | None = None


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
        schedule_id: str | None = None,
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
            schedule_id=schedule_id,
        )

    def get_rehydration_context(
        self,
        project: str,
        recent_window_size: int = 10,
        *,
        tz: tzinfo | None = None,
        now: datetime | None = None,
    ) -> str:
        session_id = self.primary_session_id_for_project(project)
        turns_data = self.trace_store.get_primary_session_turns(session_id)
        if not turns_data:
            return ""

        all_turns = [PrimarySessionTurn(**d) for d in turns_data]
        # The window counts real conversation only; schedule firings get their own
        # block below instead of crowding it out.
        turns = [t for t in all_turns if t.turn_type not in SCHEDULE_TURN_TYPES]
        sections: list[str] = []
        if turns:
            if len(turns) <= recent_window_size:
                recent_turns = turns
                older_lines: list[str] = []
            else:
                recent_turns = turns[-recent_window_size:]
                older_turns = turns[:-recent_window_size]
                substantive_older = [
                    t for t in older_turns if t.turn_type in {"decision", "dev_report", "discussion"}
                ]
                older_lines = [f"- [{t.speaker}]: {t.content[:150]}" for t in substantive_older[-5:]]
            if older_lines:
                sections.append("Earlier substantive context:\n" + "\n".join(older_lines))
            recent_formatted = "\n".join(f"[{t.speaker}]: {t.content}" for t in recent_turns)
            sections.append(f"Recent transcript:\n{recent_formatted}")
        scheduled = self._scheduled_runs_block(all_turns, tz or timezone.utc, now)
        if scheduled:
            sections.append(scheduled)
        return "\n\n".join(sections)

    @staticmethod
    def _scheduled_runs_block(turns: list[PrimarySessionTurn], tz: tzinfo, now: datetime | None) -> str:
        """Latest result per schedule that ran recently. These were pushed to the human
        on Telegram, so a reply like "那檔後來怎麼了" may be about one of them even though
        none sits in the conversation window."""
        cutoff = (now or datetime.now(timezone.utc)) - SCHEDULED_RUNS_LOOKBACK
        by_schedule: dict[str, dict] = {}
        for turn in turns:
            if turn.turn_type not in SCHEDULE_TURN_TYPES or not turn.schedule_id:
                continue
            created = datetime.fromisoformat(turn.created_at)
            if created < cutoff:
                continue
            entry = by_schedule.setdefault(
                turn.schedule_id, {"goal": "", "fired": 0, "last_seen": created, "report": None}
            )
            entry["last_seen"] = max(entry["last_seen"], created)
            if turn.turn_type == SCHEDULE_TRIGGER:
                entry["fired"] += 1
                entry["goal"] = turn.content.partition("觸發：")[2].strip() or entry["goal"]
            else:
                entry["report"] = (created, turn.content)
        if not by_schedule:
            return ""
        newest_first = sorted(by_schedule.items(), key=lambda kv: kv[1]["last_seen"], reverse=True)
        lines = []
        for schedule_id, entry in newest_first[:SCHEDULED_RUNS_MAX]:
            head = f"- {schedule_id}"
            if entry["goal"]:
                head += f"「{entry['goal'][:80]}」"
            head += f"，近 24 小時觸發 {entry['fired']} 次"
            if entry["report"]:
                created, content = entry["report"]
                stamp = created.astimezone(tz).strftime("%m-%d %H:%M")
                lines.append(f"{head}；最近一次結果（{stamp}）：{content[:SCHEDULED_RESULT_CHARS]}")
            else:
                lines.append(f"{head}；這段期間沒有執行結果")
        return (
            "Scheduled runs in the last 24 hours (latest result per schedule — the Harness "
            "pushed these to the human on Telegram, so their next message may refer to "
            "them):\n" + "\n".join(lines)
        )

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
        tz: tzinfo | None = None,
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

        all_turns = [PrimarySessionTurn(**d) for d in unconsolidated_data]
        # Schedule firings and their results are never written to MemTrace hourly: they
        # repeat every few minutes and say nothing a draft note needs. They are marked
        # done right away (nothing is left pending for them) and reach long-term memory
        # through the nightly digest, which keeps only each schedule's last result.
        schedule_ids = [t.id for t in all_turns if t.turn_type in SCHEDULE_TURN_TYPES]
        if schedule_ids:
            self.trace_store.mark_turns_consolidated(schedule_ids)
        turns = [t for t in all_turns if t.turn_type not in SCHEDULE_TURN_TYPES]
        if not turns:
            return []
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

        # In turn order: the body and the title's turn range read as one stretch of
        # conversation, not "decisions first, then whichever chat turns got promoted".
        substantive = sorted(deterministic + promoted_chat, key=lambda t: t.turn_seq)
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
            # Titled by when and which turns (see memory_drafts.py): the same title for
            # every draft made them indistinguishable in MemTrace.
            node_id = self.memtrace_client.create_node(
                workspace_id=workspace_id,
                title=draft_title(
                    project,
                    [(t.turn_seq, t.created_at, t.speaker, t.content) for t in substantive],
                    tz or timezone.utc,
                ),
                body=summary_content,
                content_type="context",
                tags=["harness", "draft", "primary-session"],
                force_create=True,
                stage="consolidation",
            )
            # Remember which node holds which turns, so the nightly digest can link to it.
            self.trace_store.record_memory_draft(
                project=project,
                workspace_id=workspace_id,
                node_id=str(node_id),
                first_turn_seq=substantive[0].turn_seq,
                last_turn_seq=substantive[-1].turn_seq,
            )

        self.trace_store.mark_turns_consolidated([t.id for t in turns])
        return [t.id for t in substantive]
