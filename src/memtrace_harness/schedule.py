"""Pure scheduling logic for the chat-triggered recurring-task feature: parsing a
schedule directive out of a model reply, and computing when it should next fire.
No I/O here — persistence lives in TraceStore, triggering lives in TelegramGateway."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

VALID_KINDS = {"interval", "daily", "weekdays"}

_MIN_INTERVAL_SECONDS = 60
_WEEKEND_DAYS = (5, 6)  # datetime.weekday(): Saturday=5, Sunday=6


@dataclass(frozen=True)
class ScheduleSpec:
    kind: str  # "interval" | "daily" | "weekdays"
    interval_seconds: int | None = None
    time_of_day: str | None = None  # "HH:MM", 24-hour, local to the schedule's timezone
    end_time_of_day: str | None = None  # "HH:MM"; daily/weekdays only, stops new starts after this


def _parse_time_of_day(value: str, kind: str) -> str:
    hour_str, sep, minute_str = value.partition(":")
    valid = (
        sep == ":"
        and hour_str.isdigit()
        and minute_str.isdigit()
        and 0 <= int(hour_str) <= 23
        and 0 <= int(minute_str) <= 59
    )
    if not valid:
        raise ValueError(f"{kind} 排程時間必須是 24 小時制的 HH:MM，收到「{value}」")
    return f"{int(hour_str):02d}:{int(minute_str):02d}"


def parse_schedule_spec(kind: str, spec: str) -> ScheduleSpec:
    """Parse the "<kind>::<spec>" portion of a HARNESS_SCHEDULE_START directive.
    For "daily"/"weekdays", spec is either "HH:MM" (fires once, no cutoff) or
    "HH:MM-HH:MM" (fires at the start time, and stops starting new runs once the
    end time has passed for the day — an already-running task is not interrupted).
    Raises ValueError with a human-readable message on anything malformed —
    callers surface that message straight back to the chat, so it must stay
    understandable without extra context."""
    kind = kind.strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"不認得的排程種類「{kind}」（可用：{'、'.join(sorted(VALID_KINDS))}）")

    if kind == "interval":
        try:
            seconds = int(spec.strip())
        except ValueError as exc:
            raise ValueError(f"interval 排程的週期必須是整數秒數，收到「{spec}」") from exc
        if seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval 排程週期至少要 {_MIN_INTERVAL_SECONDS} 秒")
        return ScheduleSpec(kind="interval", interval_seconds=seconds)

    start_str, sep, end_str = spec.strip().partition("-")
    start = _parse_time_of_day(start_str, kind)
    end_time_of_day = _parse_time_of_day(end_str, kind) if sep == "-" else None
    if end_time_of_day is not None and end_time_of_day <= start:
        raise ValueError(f"{kind} 排程的結束時間必須晚於起始時間，收到「{spec}」")
    return ScheduleSpec(kind=kind, time_of_day=start, end_time_of_day=end_time_of_day)


def compute_next_run(spec: ScheduleSpec, *, after: datetime, tz: ZoneInfo) -> datetime:
    """Next UTC instant a schedule should fire, strictly after `after` (must be
    timezone-aware). `interval` is relative to `after`; `daily`/`weekdays` are
    anchored to the wall-clock `time_of_day` in `tz`, skipping Sat/Sun for
    `weekdays`."""
    if spec.kind == "interval":
        assert spec.interval_seconds is not None
        return after + timedelta(seconds=spec.interval_seconds)

    assert spec.time_of_day is not None
    hour, minute = (int(part) for part in spec.time_of_day.split(":"))
    local_after = after.astimezone(tz)
    candidate = local_after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_after:
        candidate += timedelta(days=1)
    if spec.kind == "weekdays":
        while candidate.weekday() in _WEEKEND_DAYS:
            candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def is_past_window_end(spec: ScheduleSpec, *, at: datetime, tz: ZoneInfo) -> bool:
    """True when `at`'s wall-clock time in `tz` is already past the schedule's
    end_time_of_day for today. Used to skip *starting* a new run once a
    daily/weekdays schedule's window has closed for the day — it never touches
    an already-running task, which keeps running to its own completion."""
    if spec.end_time_of_day is None:
        return False
    local_at = at.astimezone(tz)
    hour, minute = (int(part) for part in spec.end_time_of_day.split(":"))
    end_today = local_at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local_at > end_today


def describe_schedule(spec: ScheduleSpec) -> str:
    """Human-readable (Traditional Chinese) summary for chat messages / /schedules."""
    if spec.kind == "interval":
        return f"每 {spec.interval_seconds} 秒"
    window = f"{spec.time_of_day}~{spec.end_time_of_day}" if spec.end_time_of_day else spec.time_of_day
    if spec.kind == "daily":
        return f"每天 {window}"
    return f"每個工作日 {window}"
