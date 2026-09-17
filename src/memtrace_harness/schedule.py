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


def parse_schedule_spec(kind: str, spec: str) -> ScheduleSpec:
    """Parse the "<kind>::<spec>" portion of a HARNESS_SCHEDULE_START directive.
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

    time_of_day = spec.strip()
    hour_str, sep, minute_str = time_of_day.partition(":")
    valid = (
        sep == ":"
        and hour_str.isdigit()
        and minute_str.isdigit()
        and 0 <= int(hour_str) <= 23
        and 0 <= int(minute_str) <= 59
    )
    if not valid:
        raise ValueError(f"{kind} 排程時間必須是 24 小時制的 HH:MM，收到「{spec}」")
    return ScheduleSpec(kind=kind, time_of_day=f"{int(hour_str):02d}:{int(minute_str):02d}")


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


def describe_schedule(spec: ScheduleSpec) -> str:
    """Human-readable (Traditional Chinese) summary for chat messages / /schedules."""
    if spec.kind == "interval":
        return f"每 {spec.interval_seconds} 秒"
    if spec.kind == "daily":
        return f"每天 {spec.time_of_day}"
    return f"每個工作日 {spec.time_of_day}"
