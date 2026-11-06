"""Cron-style schedules, written from scratch (no extra library).

A schedule is five fields:  minute  hour  day-of-month  month  day-of-week

    0 2 * * *        every day at 02:00
    */15 * * * *     every 15 minutes
    30 3 * * sun     Sundays at 03:30
    0 9 1-7 * mon    see the note on days below
    @daily           same as  0 0 * * *

Each field can be:  *   a number   a-b (range)   a,b,c (list)   */n or a-b/n (every n)
Months can be written jan..dec and weekdays sun..sat. Weekday 0 and 7 are both Sunday.
Macros: @yearly @annually @monthly @weekly @daily @midnight @hourly.

Note on days (a famous cron quirk, kept on purpose so it behaves like cron on
every other system): if you restrict BOTH day-of-month and day-of-week, a day
matches when EITHER does. "0 9 1-7 * mon" means "the 1st to the 7th, OR any
Monday", not "the first Monday". If either field is a plain `*` (or starts
with one, like `*/2`), only the other restricts the day.

Times are in a time zone you choose (an IANA name such as Europe/Berlin).
When clocks go forward, a run scheduled inside the skipped hour happens an
hour later that day. When clocks go back, a run scheduled inside the repeated
hour happens once (the first time).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from yourtool.config import ConfigError


class CronError(ConfigError):
    """The schedule text is not valid."""


_MONTH_NAMES = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# Longest each month can be (February: 29, because leap years exist).
_MOST_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

_SEARCH_YEARS = 9  # covers the longest gap between February 29ths (2096 -> 2104)


@dataclass(frozen=True)
class _FieldSpec:
    name: str
    low: int
    high: int  # highest number accepted when written explicitly
    star_high: int  # highest number that `*` expands to
    names: tuple[str, ...] = ()
    names_start: int = 0


_FIELDS = (
    _FieldSpec("minute", 0, 59, 59),
    _FieldSpec("hour", 0, 23, 23),
    _FieldSpec("day-of-month", 1, 31, 31),
    _FieldSpec("month", 1, 12, 12, _MONTH_NAMES, 1),
    _FieldSpec("day-of-week", 0, 7, 6, _DAY_NAMES, 0),
)


def _number(token: str, spec: _FieldSpec) -> int:
    if token.isdigit():
        return int(token)
    if spec.names and token.lower() in spec.names:
        return spec.names.index(token.lower()) + spec.names_start
    hint = f" (or a name such as {spec.names[0]})" if spec.names else ""
    raise CronError(f"'{token}' is not a valid {spec.name} value; use a number{hint}.")


def _parse_field(text: str, spec: _FieldSpec) -> tuple[frozenset[int], bool]:
    """Returns the matching numbers, and whether the field starts with `*`."""
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"The {spec.name} field has an empty item: '{text}'.")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(
                    f"In the {spec.name} field, the step in '{part}' must be a whole number of 1 or more."
                )
            step = int(step_text)

        if base == "*":
            low, high = spec.low, spec.star_high
        elif "-" in base:
            first, _, last = base.partition("-")
            low, high = _number(first, spec), _number(last, spec)
            if low > high:
                raise CronError(
                    f"In the {spec.name} field, the range '{base}' goes backwards "
                    "(write the smaller number first)."
                )
        else:
            if slash:
                raise CronError(
                    f"In the {spec.name} field, a step needs '*' or a range, "
                    f"like '*/5' or '1-30/5', not '{part}'."
                )
            low = high = _number(base, spec)

        for number in (low, high):
            if not spec.low <= number <= spec.high:
                raise CronError(
                    f"The {spec.name} value {number} is out of range ({spec.low}-{spec.high})."
                )
        values.update(range(low, high + 1, step))

    if spec.name == "day-of-week":
        values = {0 if v == 7 else v for v in values}  # 7 is also Sunday
    return frozenset(values), text.startswith("*")


@dataclass(frozen=True)
class CronExpression:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # 0 = Sunday
    dom_is_star: bool
    dow_is_star: bool

    @classmethod
    def parse(cls, text: str) -> "CronExpression":
        original = text
        text = text.strip()
        if text.startswith("@"):
            if text.lower() not in _MACROS:
                known = ", ".join(sorted(_MACROS))
                raise CronError(f"Unknown schedule macro '{text}'. Supported: {known}.")
            text = _MACROS[text.lower()]
        parts = text.split()
        if len(parts) != 5:
            raise CronError(
                f"A schedule needs 5 fields (minute hour day-of-month month day-of-week), "
                f"but '{original}' has {len(parts)}."
            )
        (minutes, _), (hours, _), (doms, dom_star), (months, _), (dows, dow_star) = (
            _parse_field(part, spec) for part, spec in zip(parts, _FIELDS)
        )
        expression = cls(
            text=original.strip(), minutes=minutes, hours=hours, days_of_month=doms,
            months=months, days_of_week=dows, dom_is_star=dom_star, dow_is_star=dow_star,
        )
        expression._check_it_can_ever_run()
        return expression

    def _check_it_can_ever_run(self) -> None:
        if self.dom_is_star or not self.dow_is_star:
            return  # days of the week can always match something
        if not any(min(self.days_of_month) <= _MOST_DAYS[m] for m in self.months):
            raise CronError(
                f"'{self.text}' can never run: none of those months has a day "
                f"{min(self.days_of_month)} or later."
            )

    # ------------------------------------------------------------- matching

    def _day_matches(self, day: datetime) -> bool:
        dom_ok = day.day in self.days_of_month
        dow_ok = (day.weekday() + 1) % 7 in self.days_of_week  # Python: Monday=0; cron: Sunday=0
        if self.dom_is_star or self.dow_is_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok  # both restricted: either one is enough

    def matches(self, moment: datetime) -> bool:
        """Does this (wall clock) minute match? Seconds and time zone are ignored."""
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and self._day_matches(moment)
        )

    # ----------------------------------------------------------- next run

    def _next_wall_time(self, start: datetime) -> datetime | None:
        """First matching wall-clock minute at or after ``start`` (naive times)."""
        current = start.replace(second=0, microsecond=0)
        limit = current + timedelta(days=366 * _SEARCH_YEARS)
        while current < limit:
            if current.month not in self.months:
                current = (current.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            elif not self._day_matches(current):
                current = (current + timedelta(days=1)).replace(hour=0, minute=0)
            elif current.hour not in self.hours:
                current = (current + timedelta(hours=1)).replace(minute=0)
            elif current.minute not in self.minutes:
                later = [m for m in self.minutes if m > current.minute]
                if later:
                    current = current.replace(minute=min(later))
                else:
                    current = (current + timedelta(hours=1)).replace(minute=0)
            else:
                return current
        return None

    def next_after(self, after: datetime, tz: tzinfo = timezone.utc) -> datetime:
        """The first run time strictly after ``after``, as a time in ``tz``."""
        if after.tzinfo is None:
            raise ValueError("'after' must be a timezone-aware datetime.")
        after_utc = after.astimezone(timezone.utc)
        start = after.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(10_000):
            wall = self._next_wall_time(start)
            if wall is None:
                raise CronError(f"'{self.text}' never runs.")
            moment = wall.replace(tzinfo=tz)  # a time that is skipped or repeated resolves sensibly
            moment_utc = moment.astimezone(timezone.utc)
            if moment_utc > after_utc:
                return moment_utc.astimezone(tz)  # normalised: a skipped time shows as the later hour
            start = wall + timedelta(minutes=1)
        raise CronError(f"Could not work out the next run of '{self.text}'.")

    def next_runs(self, after: datetime, tz: tzinfo, count: int) -> list[datetime]:
        runs = []
        moment = after
        for _ in range(count):
            moment = self.next_after(moment, tz)
            runs.append(moment)
        return runs
