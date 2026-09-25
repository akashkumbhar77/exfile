"""Cron fields get their standard meaning, once, for every consumer of a `schedule` trigger."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import cron


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_fields_expand_to_explicit_values() -> None:
    s = cron.parse("15 9-17/4 1,15 JAN-MAR MON-FRI")
    assert s.fixed_minute == 15
    assert s.hours.values == {9, 13, 17}
    assert s.days.values == {1, 15}
    assert s.months.values == {1, 2, 3}
    assert s.weekdays.values == {1, 2, 3, 4, 5}


def test_a_star_does_not_restrict_but_a_stepped_star_does() -> None:
    s = cron.parse("0 */6 * * *")
    assert s.hours.restricted and s.hours.values == {0, 6, 12, 18}
    assert not s.days.restricted and not s.weekdays.restricted


def test_seven_is_sunday_too() -> None:
    assert cron.parse("0 9 * * 7").weekdays.values == {0}
    assert cron.parse("0 9 * * 5-7").weekdays.values == {5, 6, 0}


def test_minute_lists_and_steps_are_finer_than_hourly() -> None:
    assert cron.parse("*/15 * * * *").fixed_minute is None
    assert cron.parse("0,30 * * * *").fixed_minute is None
    assert cron.parse("30 * * * *").fixed_minute == 30


def test_matching_an_hour() -> None:
    weekdays_at_nine = cron.parse("0 9 * * MON-FRI")
    assert weekdays_at_nine.matches_hour(at("2026-09-25T09:40:00"))      # a Friday
    assert not weekdays_at_nine.matches_hour(at("2026-09-26T09:00:00"))  # Saturday
    assert not weekdays_at_nine.matches_hour(at("2026-09-25T10:00:00"))


def test_day_of_month_or_day_of_week_when_both_are_restricted() -> None:
    """Vixie cron: '0 9 1 * MON' runs on the 1st AND on every Monday, not only Monday the 1st."""
    s = cron.parse("0 9 1 * MON")
    assert s.matches_hour(at("2026-10-01T09:00:00"))   # the 1st (a Thursday)
    assert s.matches_hour(at("2026-09-28T09:00:00"))   # a Monday
    assert not s.matches_hour(at("2026-09-29T09:00:00"))


@pytest.mark.parametrize("expr", [
    "0 24 * * *", "60 * * * *", "0 9 0 * *", "0 9 * 13 *", "0 9 * * 8",
    "0 9-5 * * *", "0 */0 * * *", "0 9 * * FUNDAY", "0 9 * *", "0 9 1,,2 * *",
])
def test_unreadable_expressions_are_rejected(expr: str) -> None:
    with pytest.raises(cron.CronError):
        cron.parse(expr)


@pytest.mark.parametrize(("expr", "words"), [
    ("0 9 * * *", "every day, once between 09:00 and 10:00"),
    ("30 * * * *", "once every hour"),
    ("0 9 * * MON-FRI", "every Monday to Friday, once between 09:00 and 10:00"),
    ("0 18 * * 0,6", "every Saturday and Sunday, once between 18:00 and 19:00"),
    ("0 7 1,15 * *", "on the 1st and 15th of the month, once between 07:00 and 08:00"),
    ("0 9,13 * * *", "every day, once in each of these hours: 09:00 and 13:00"),
])
def test_schedules_in_words_never_promise_an_exact_minute(expr: str, words: str) -> None:
    text = cron.describe(expr)
    assert text.startswith(words) and text.endswith("at a minute Google picks, in the script's timezone")
