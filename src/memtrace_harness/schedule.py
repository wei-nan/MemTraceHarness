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
    weekdays_only: bool = False  # interval only: never fires on Sat/Sun


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
        seconds_str, *modifiers = (part.strip() for part in spec.split("@"))
        try:
            seconds = int(seconds_str)
        except ValueError as exc:
            raise ValueError(f"interval 排程的週期必須是整數秒數，收到「{spec}」") from exc
        if seconds < _MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval 排程週期至少要 {_MIN_INTERVAL_SECONDS} 秒")
        start = end = None
        weekdays_only = False
        for modifier in modifiers:
            if modifier == "weekdays":
                weekdays_only = True
                continue
            window_start, sep, window_end = modifier.partition("-")
            if sep != "-" or start is not None:
                raise ValueError(
                    f"interval 排程的限制只能是 @HH:MM-HH:MM（每天的執行時段）或 @weekdays，收到「{modifier}」"
                )
            start = _parse_time_of_day(window_start, kind)
            end = _parse_time_of_day(window_end, kind)
            if end <= start:
                raise ValueError(f"interval 排程的結束時間必須晚於起始時間，收到「{spec}」")
        return ScheduleSpec(
            kind="interval",
            interval_seconds=seconds,
            time_of_day=start,
            end_time_of_day=end,
            weekdays_only=weekdays_only,
        )

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
        candidate = after + timedelta(seconds=spec.interval_seconds)
        if spec.time_of_day is None and not spec.weekdays_only:
            return candidate
        return _next_in_interval_window(spec, candidate, tz)

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


def _window_bounds(spec: ScheduleSpec, local: datetime) -> tuple[datetime, datetime]:
    start_h, start_m = (int(p) for p in (spec.time_of_day or "00:00").split(":"))
    end_h, end_m = (int(p) for p in (spec.end_time_of_day or "23:59").split(":"))
    return (
        local.replace(hour=start_h, minute=start_m, second=0, microsecond=0),
        local.replace(hour=end_h, minute=end_m, second=0, microsecond=0),
    )


def _next_in_interval_window(spec: ScheduleSpec, candidate: datetime, tz: ZoneInfo) -> datetime:
    """First instant at or after `candidate` that is inside an interval schedule's daily
    window (and on a weekday when weekdays_only): a firing that would land after the
    window or on a weekend moves to the next window start instead of happening."""
    local = candidate.astimezone(tz)
    for _ in range(10):
        start, end = _window_bounds(spec, local)
        if spec.weekdays_only and local.weekday() in _WEEKEND_DAYS:
            local = (start + timedelta(days=1))
            continue
        if local < start:
            local = start
        if local > end:
            local = start + timedelta(days=1)
            continue
        return local.astimezone(timezone.utc)
    return candidate


def is_outside_interval_window(spec: ScheduleSpec, *, at: datetime, tz: ZoneInfo) -> bool:
    """True when an interval schedule with a window and/or weekday restriction is due
    at a moment outside it (e.g. a gateway that was down and caught up late)."""
    if spec.kind != "interval" or (spec.time_of_day is None and not spec.weekdays_only):
        return False
    return _next_in_interval_window(spec, at, tz) != at.astimezone(timezone.utc)


def spec_from_row(row: dict) -> ScheduleSpec:
    return ScheduleSpec(
        kind=row["kind"],
        interval_seconds=row["interval_seconds"],
        time_of_day=row["time_of_day"],
        end_time_of_day=row.get("end_time_of_day"),
        weekdays_only=bool(row.get("weekdays_only")),
    )


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
        text = f"每 {spec.interval_seconds} 秒"
        if spec.time_of_day:
            text += f"，只在 {spec.time_of_day}~{spec.end_time_of_day}"
        if spec.weekdays_only:
            text += "，僅工作日"
        return text
    window = f"{spec.time_of_day}~{spec.end_time_of_day}" if spec.end_time_of_day else spec.time_of_day
    if spec.kind == "daily":
        return f"每天 {window}"
    return f"每個工作日 {window}"
