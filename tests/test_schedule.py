from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from zoneinfo import ZoneInfo

from memtrace_harness.schedule import (
    ScheduleSpec,
    compute_next_run,
    describe_schedule,
    parse_schedule_spec,
)

TAIPEI = ZoneInfo("Asia/Taipei")


class ParseScheduleSpecTests(TestCase):
    def test_interval_parses_seconds(self) -> None:
        spec = parse_schedule_spec("interval", "3600")
        self.assertEqual(spec, ScheduleSpec(kind="interval", interval_seconds=3600))

    def test_interval_rejects_below_minimum(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("interval", "10")

    def test_interval_rejects_non_numeric(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("interval", "soon")

    def test_daily_parses_and_normalizes_time(self) -> None:
        spec = parse_schedule_spec("daily", "9:5")
        self.assertEqual(spec, ScheduleSpec(kind="daily", time_of_day="09:05"))

    def test_weekdays_parses_time(self) -> None:
        spec = parse_schedule_spec("WEEKDAYS", "09:00")
        self.assertEqual(spec, ScheduleSpec(kind="weekdays", time_of_day="09:00"))

    def test_daily_rejects_bad_time(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("daily", "25:00")

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("monthly", "1")


class ComputeNextRunTests(TestCase):
    def test_interval_adds_seconds_to_after(self) -> None:
        after = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)
        spec = ScheduleSpec(kind="interval", interval_seconds=1800)
        self.assertEqual(compute_next_run(spec, after=after, tz=TAIPEI), after.replace(minute=30))

    def test_daily_rolls_to_tomorrow_if_time_already_passed_today(self) -> None:
        # 2026-09-17 10:00 Asia/Taipei (UTC+8) has already passed 09:00 today.
        after = datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc)
        spec = ScheduleSpec(kind="daily", time_of_day="09:00")
        next_run = compute_next_run(spec, after=after, tz=TAIPEI)
        local = next_run.astimezone(TAIPEI)
        self.assertEqual((local.year, local.month, local.day), (2026, 9, 18))
        self.assertEqual((local.hour, local.minute), (9, 0))

    def test_daily_uses_today_if_time_still_ahead(self) -> None:
        # 2026-09-17 07:00 Asia/Taipei is still before 09:00 today.
        after = datetime(2026, 9, 16, 23, 0, tzinfo=timezone.utc)
        spec = ScheduleSpec(kind="daily", time_of_day="09:00")
        next_run = compute_next_run(spec, after=after, tz=TAIPEI)
        local = next_run.astimezone(TAIPEI)
        self.assertEqual((local.year, local.month, local.day), (2026, 9, 17))

    def test_weekdays_skips_weekend(self) -> None:
        # 2026-09-18 is a Friday in Taipei; the next weekday occurrence after
        # today's time has passed must land on Monday 2026-09-21, not Saturday.
        after = datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc)  # 2026-09-18 10:00 Taipei
        spec = ScheduleSpec(kind="weekdays", time_of_day="09:00")
        next_run = compute_next_run(spec, after=after, tz=TAIPEI)
        local = next_run.astimezone(TAIPEI)
        self.assertEqual((local.year, local.month, local.day), (2026, 9, 21))
        self.assertEqual(local.weekday(), 0)  # Monday


class DescribeScheduleTests(TestCase):
    def test_interval_description(self) -> None:
        self.assertIn("3600", describe_schedule(ScheduleSpec(kind="interval", interval_seconds=3600)))

    def test_daily_description(self) -> None:
        self.assertIn("09:00", describe_schedule(ScheduleSpec(kind="daily", time_of_day="09:00")))

    def test_weekdays_description(self) -> None:
        desc = describe_schedule(ScheduleSpec(kind="weekdays", time_of_day="09:00"))
        self.assertIn("工作日", desc)
        self.assertIn("09:00", desc)
