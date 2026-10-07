from __future__ import annotations

from datetime import datetime, timezone
from unittest import TestCase
from zoneinfo import ZoneInfo

from memtrace_harness.schedule import (
    ScheduleSpec,
    compute_next_run,
    describe_schedule,
    is_outside_interval_window,
    is_past_window_end,
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

    def test_weekdays_parses_window(self) -> None:
        spec = parse_schedule_spec("weekdays", "9:0-13:25")
        self.assertEqual(
            spec, ScheduleSpec(kind="weekdays", time_of_day="09:00", end_time_of_day="13:25")
        )

    def test_window_rejects_end_before_start(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("weekdays", "13:25-09:00")

    def test_window_rejects_bad_end_time(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("daily", "09:00-25:00")


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


class IsPastWindowEndTests(TestCase):
    def test_no_end_time_never_past_window(self) -> None:
        spec = ScheduleSpec(kind="weekdays", time_of_day="09:00")
        at = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)  # 18:00 Taipei
        self.assertFalse(is_past_window_end(spec, at=at, tz=TAIPEI))

    def test_before_end_time_is_not_past_window(self) -> None:
        spec = ScheduleSpec(kind="weekdays", time_of_day="09:00", end_time_of_day="13:25")
        at = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)  # 09:00 Taipei
        self.assertFalse(is_past_window_end(spec, at=at, tz=TAIPEI))

    def test_after_end_time_is_past_window(self) -> None:
        spec = ScheduleSpec(kind="weekdays", time_of_day="09:00", end_time_of_day="13:25")
        at = datetime(2026, 9, 22, 5, 31, tzinfo=timezone.utc)  # 13:31 Taipei
        self.assertTrue(is_past_window_end(spec, at=at, tz=TAIPEI))


class DescribeScheduleTests(TestCase):
    def test_interval_description(self) -> None:
        self.assertIn("3600", describe_schedule(ScheduleSpec(kind="interval", interval_seconds=3600)))

    def test_daily_description(self) -> None:
        self.assertIn("09:00", describe_schedule(ScheduleSpec(kind="daily", time_of_day="09:00")))

    def test_weekdays_description(self) -> None:
        desc = describe_schedule(ScheduleSpec(kind="weekdays", time_of_day="09:00"))
        self.assertIn("工作日", desc)
        self.assertIn("09:00", desc)

    def test_weekdays_description_includes_window(self) -> None:
        desc = describe_schedule(
            ScheduleSpec(kind="weekdays", time_of_day="09:00", end_time_of_day="13:25")
        )
        self.assertIn("09:00~13:25", desc)


class IntervalWindowTest(TestCase):
    TZ = ZoneInfo("Asia/Taipei")

    def _spec(self) -> ScheduleSpec:
        return parse_schedule_spec("interval", "600@09:00-13:30@weekdays")

    def _local(self, text: str) -> datetime:
        return datetime.fromisoformat(text).replace(tzinfo=self.TZ).astimezone(timezone.utc)

    def test_parses_window_and_weekdays(self) -> None:
        spec = self._spec()
        self.assertEqual((spec.interval_seconds, spec.time_of_day, spec.end_time_of_day, spec.weekdays_only),
                         (600, "09:00", "13:30", True))
        self.assertIn("09:00~13:30", describe_schedule(spec))

    def test_rejects_a_bad_window(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule_spec("interval", "600@13:30-09:00")
        with self.assertRaises(ValueError):
            parse_schedule_spec("interval", "600@whenever")

    def test_inside_the_window_fires_on_the_interval(self) -> None:
        nxt = compute_next_run(self._spec(), after=self._local("2026-10-07T10:00:00"), tz=self.TZ)
        self.assertEqual(nxt, self._local("2026-10-07T10:10:00"))

    def test_after_the_window_moves_to_next_trading_day_open(self) -> None:
        nxt = compute_next_run(self._spec(), after=self._local("2026-10-07T13:25:00"), tz=self.TZ)
        self.assertEqual(nxt, self._local("2026-10-08T09:00:00"))

    def test_friday_after_close_skips_the_weekend(self) -> None:
        nxt = compute_next_run(self._spec(), after=self._local("2026-10-09T13:28:00"), tz=self.TZ)
        self.assertEqual(nxt, self._local("2026-10-12T09:00:00"))

    def test_a_late_catch_up_outside_the_window_is_flagged(self) -> None:
        self.assertTrue(is_outside_interval_window(self._spec(), at=self._local("2026-10-07T13:42:00"), tz=self.TZ))
        self.assertFalse(is_outside_interval_window(self._spec(), at=self._local("2026-10-07T13:20:00"), tz=self.TZ))
        self.assertFalse(is_outside_interval_window(parse_schedule_spec("interval", "600"), at=self._local("2026-10-07T23:00:00"), tz=self.TZ))
