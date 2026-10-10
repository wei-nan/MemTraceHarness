from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Callable


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
        in the chat, so a reply like "那檔後來怎麼了" may be about one of them even though
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
            "pushed these to the human in the chat, so their next message may refer to "
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
