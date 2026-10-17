from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local"))

from plx.cron import CronError, CronExpression  # noqa: E402


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=timezone.utc)


class CronExpressionTests(unittest.TestCase):
    def test_daily_schedule_next_and_previous(self) -> None:
        cron = CronExpression("30 2 * * *")
        self.assertEqual(cron.next_after(utc(2026, 9, 23, 17, 0)), utc(2026, 9, 24, 2, 30))
        self.assertEqual(cron.next_after(utc(2026, 9, 24, 2, 30)), utc(2026, 9, 25, 2, 30))
        self.assertEqual(cron.previous_at_or_before(utc(2026, 9, 24, 2, 30, 45)), utc(2026, 9, 24, 2, 30))
        self.assertEqual(cron.previous_at_or_before(utc(2026, 9, 24, 2, 29)), utc(2026, 9, 23, 2, 30))

    def test_naive_datetimes_are_utc(self) -> None:
        cron = CronExpression("0 12 * * *")
        self.assertTrue(cron.matches(datetime(2026, 9, 24, 12, 0)))
        self.assertFalse(cron.matches(datetime(2026, 9, 24, 12, 0, tzinfo=timezone(timedelta(hours=8)))))

    def test_steps_ranges_lists_and_names(self) -> None:
        cron = CronExpression("*/15 9-17 * JAN-MAR MON,WED,FRI")
        self.assertTrue(cron.matches(utc(2026, 1, 5, 9, 45)))  # Monday
        self.assertFalse(cron.matches(utc(2026, 1, 6, 9, 45)))  # Tuesday
        self.assertFalse(cron.matches(utc(2026, 4, 6, 9, 45)))  # April
        self.assertFalse(cron.matches(utc(2026, 1, 5, 9, 50)))

    def test_step_applies_to_its_own_list_item(self) -> None:
        self.assertEqual(sorted(CronExpression("0 1,12/6 * * *").hours), [1, 12, 18])
        self.assertEqual(sorted(CronExpression("5,20/10 * * * *").minutes), [5, 20, 30, 40, 50])
        self.assertEqual(sorted(CronExpression("0 0 1-10/3,15 * *").days), [1, 4, 7, 10, 15])

    def test_sunday_is_zero_or_seven(self) -> None:
        self.assertEqual(CronExpression("0 0 * * 7").weekdays, CronExpression("0 0 * * 0").weekdays)

    def test_day_of_month_or_weekday_when_both_restricted(self) -> None:
        cron = CronExpression("0 0 1 * MON")
        self.assertTrue(cron.matches(utc(2026, 9, 1, 0, 0)))  # 1st (Tuesday)
        self.assertTrue(cron.matches(utc(2026, 9, 7, 0, 0)))  # Monday
        self.assertFalse(cron.matches(utc(2026, 9, 8, 0, 0)))

    def test_month_rollover(self) -> None:
        cron = CronExpression("0 0 31 * *")
        self.assertEqual(cron.next_after(utc(2026, 9, 1)), utc(2026, 10, 31))

    def test_invalid_expressions(self) -> None:
        for expression in ("* * * *", "61 * * * *", "*/0 * * * *", "5-1 * * * *", "a * * * *"):
            with self.subTest(expression=expression):
                with self.assertRaises(CronError):
                    CronExpression(expression)


if __name__ == "__main__":
    unittest.main()
