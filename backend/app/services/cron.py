"""The 5-field cron expressions a `schedule` trigger carries, parsed into explicit value sets.

ConfigSpec only checks the shape (five whitespace-separated fields). This module gives those fields
their standard (Vixie cron) meaning, so every consumer reads a schedule the same way:

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-6 (0 or 7 = Sunday)

Each field accepts `*`, a number, a range `a-b`, a step `*/n` or `a-b/n`, and comma lists of
those; months and weekdays also accept three-letter names (JAN, MON). When both day-of-month and
day-of-week are restricted, a day matches if EITHER matches (cron's long-standing rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_WEEKDAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class CronField:
    values: frozenset[int]
    restricted: bool  # False for `*` (and `*/1`): the field does not narrow anything

    def __contains__(self, value: int) -> bool:
        return value in self.values


@dataclass(frozen=True)
class CronSchedule:
    minutes: CronField
    hours: CronField
    days: CronField
    months: CronField
    weekdays: CronField  # 0 = Sunday

    def day_matches(self, day: int, weekday: int) -> bool:
        if self.days.restricted and self.weekdays.restricted:
            return day in self.days or weekday in self.weekdays
        return day in self.days and weekday in self.weekdays

    def matches_hour(self, when: datetime) -> bool:
        """Whether the schedule fires at some minute of the hour containing `when` (its own zone)."""
        weekday = (when.weekday() + 1) % 7  # Python: Monday=0; cron: Sunday=0
        return (when.hour in self.hours and when.month in self.months
                and self.day_matches(when.day, weekday))

    @property
    def fixed_minute(self) -> int | None:
        """The one minute of the hour it runs at, or None when it runs at several (finer than hourly)."""
        return next(iter(self.minutes.values)) if len(self.minutes.values) == 1 else None


def _number(text: str, lo: int, hi: int, names: list[str] | None, what: str) -> int:
    upper = text.upper()
    if names is not None and upper in names:
        return names.index(upper) + (1 if what == "month" else 0)
    if not text.isdigit():
        raise CronError(f"{what} field: {text!r} is not a number")
    number = int(text)
    if what == "day-of-week" and number == 7:
        number = 0
    if not lo <= number <= hi:
        raise CronError(f"{what} field: {number} is outside {lo}-{hi}")
    return number


def _field(text: str, lo: int, hi: int, what: str, names: list[str] | None = None) -> CronField:
    top = 7 if what == "day-of-week" else hi
    values: set[int] = set()
    restricted = False
    for part in text.split(","):
        if not part:
            raise CronError(f"{what} field: empty list item in {text!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"{what} field: bad step {step_text!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
            restricted = restricted or step != 1
        elif "-" in base:
            a, _, b = base.partition("-")
            start = _number(a, lo, top, names, what)
            end = _number(b, lo, top, names, what)
            if what == "day-of-week" and b == "7":
                end = 7
            if start > end:
                raise CronError(f"{what} field: range {base!r} runs backwards")
            restricted = True
        else:
            start = _number(base, lo, top, names, what)
            end = hi if step_text else start
            restricted = True
        values.update(v % 7 if what == "day-of-week" else v for v in range(start, end + 1, step))
    return CronField(frozenset(values), restricted)


def parse(expr: str) -> CronSchedule:
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"expected 5 fields, got {len(parts)}")
    minute, hour, day, month, weekday = parts
    return CronSchedule(
        minutes=_field(minute, 0, 59, "minute"),
        hours=_field(hour, 0, 23, "hour"),
        days=_field(day, 1, 31, "day-of-month"),
        months=_field(month, 1, 12, "month", _MONTHS),
        weekdays=_field(weekday, 0, 6, "day-of-week", _WEEKDAYS),
    )


_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                "October", "November", "December"]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _weekdays(values: frozenset[int]) -> str:
    order = [1, 2, 3, 4, 5, 6, 0]  # Monday first
    chosen = [d for d in order if d in values]
    spans = [order[i:i + len(chosen)] for i in range(len(order) - len(chosen) + 1)]
    if len(chosen) >= 3 and chosen in spans:
        return f"every {_DAY_NAMES[chosen[0]]} to {_DAY_NAMES[chosen[-1]]}"
    return "every " + _join([_DAY_NAMES[d] for d in chosen])


def describe(expr: str) -> str:
    """A schedule in plain words. It says "once between 09:00 and 10:00", not "at 09:30": the
    generated script checks the schedule hourly and Google picks the minute (MOCK-DIVERGENCES #3)."""
    s = parse(expr)
    if not s.hours.restricted:
        when = "once every hour"
    elif len(s.hours.values) == 1:
        h = next(iter(s.hours.values))
        when = f"once between {h:02d}:00 and {(h + 1) % 24:02d}:00"
    else:
        when = "once in each of these hours: " + _join([f"{h:02d}:00" for h in sorted(s.hours.values)])
    if s.days.restricted and s.weekdays.restricted:
        days = f"on the {_join([_ordinal(d) for d in sorted(s.days.values)])} of the month and " \
               f"{_weekdays(s.weekdays.values)}"
    elif s.days.restricted:
        days = f"on the {_join([_ordinal(d) for d in sorted(s.days.values)])} of the month"
    elif s.weekdays.restricted:
        days = _weekdays(s.weekdays.values)
    else:
        days = "every day"
    months = f" in {_join([_MONTH_NAMES[m - 1] for m in sorted(s.months.values)])}" if s.months.restricted else ""
    lead = "" if days == "every day" and not months and not s.hours.restricted else f"{days}{months}, "
    return f"{lead}{when}, at a minute Google picks, in the script's timezone"
