"""The cron parser: what schedules mean, and when they next run."""

import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from yourtool.config import ConfigError
from yourtool.cron import CronError, CronExpression

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def parse(text):
    return CronExpression.parse(text)


# -------------------------------------------------------------- parsing


def test_a_plain_daily_schedule():
    c = parse("0 2 * * *")
    assert c.minutes == {0} and c.hours == {2}
    assert c.days_of_month == set(range(1, 32)) and c.months == set(range(1, 13))
    assert c.days_of_week == set(range(0, 7))


def test_lists_ranges_and_steps():
    assert parse("*/15 * * * *").minutes == {0, 15, 30, 45}
    assert parse("1-10/3 * * * *").minutes == {1, 4, 7, 10}
    assert parse("1,5,10 * * * *").minutes == {1, 5, 10}
    assert parse("0 9-17/4 * * *").hours == {9, 13, 17}
    assert parse("0 0 1,15 * *").days_of_month == {1, 15}


def test_month_and_weekday_names_in_any_case():
    assert parse("0 0 * jan-mar *").months == {1, 2, 3}
    assert parse("0 0 * * mon-fri").days_of_week == {1, 2, 3, 4, 5}
    assert parse("0 0 * * MON,Sat").days_of_week == {1, 6}
    assert parse("0 0 * DEC *").months == {12}


def test_weekday_7_is_sunday_too():
    assert parse("0 0 * * 7").days_of_week == {0}
    assert parse("0 0 * * 0,7").days_of_week == {0}
    assert parse("0 0 * * 5-7").days_of_week == {5, 6, 0}


@pytest.mark.parametrize(
    "macro, equivalent",
    [("@hourly", "0 * * * *"), ("@daily", "0 0 * * *"), ("@midnight", "0 0 * * *"),
     ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *"), ("@yearly", "0 0 1 1 *"),
     ("@annually", "0 0 1 1 *"), ("@DAILY", "0 0 * * *")],
)
def test_macros(macro, equivalent):
    a, b = parse(macro), parse(equivalent)
    assert (a.minutes, a.hours, a.days_of_month, a.months, a.days_of_week) == (
        b.minutes, b.hours, b.days_of_month, b.months, b.days_of_week)


def test_extra_spaces_are_fine_and_the_original_text_is_kept():
    c = parse("  0   2 *  * *  ")
    assert c.hours == {2} and c.text == "0   2 *  * *"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "needs 5 fields"),
        ("* * * *", "needs 5 fields"),
        ("* * * * * *", "needs 5 fields"),
        ("60 * * * *", "out of range"),
        ("* 24 * * *", "out of range"),
        ("* * 0 * *", "out of range"),
        ("* * 32 * *", "out of range"),
        ("* * * 13 *", "out of range"),
        ("* * * * 8", "out of range"),
        ("5-1 * * * *", "goes backwards"),
        ("*/0 * * * *", "step"),
        ("*/x * * * *", "step"),
        ("a * * * *", "not a valid minute value"),
        ("* * * foo *", "not a valid month value"),
        ("* * * * funday", "not a valid day-of-week value"),
        ("1,,2 * * * *", "empty item"),
        ("5/2 * * * *", "needs '*' or a range"),
        ("@reboot", "Unknown schedule macro"),
        ("@sometimes", "Unknown schedule macro"),
        ("0 0 31 2 *", "can never run"),
        ("0 0 30 2 *", "can never run"),
        ("0 0 31 4,6,9,11 *", "can never run"),
    ],
)
def test_invalid_schedules_say_why(text, fragment):
    with pytest.raises(CronError, match=re.escape(fragment)):
        parse(text)


def test_cron_errors_are_config_errors():
    with pytest.raises(ConfigError):
        parse("nonsense")


def test_dates_that_exist_only_sometimes_are_allowed():
    parse("0 0 29 2 *")  # leap years
    parse("0 0 31 1,3,5,7,8,10,12 *")
    parse("0 0 31 2,3 *")  # March has a 31st


# --------------------------------------------------------------- matching


def test_matches_a_wall_clock_minute():
    c = parse("30 14 * * *")
    assert c.matches(datetime(2026, 10, 24, 14, 30)) and c.matches(utc(2026, 1, 1, 14, 30, 59))
    assert not c.matches(datetime(2026, 10, 24, 14, 31))
    assert not c.matches(datetime(2026, 10, 24, 15, 30))


def test_both_day_fields_restricted_means_either_one_is_enough():
    """The classic cron rule: '0 9 1-7 * mon' = the 1st-7th OR any Monday."""
    c = parse("0 9 1-7 * mon")
    assert c.matches(datetime(2026, 10, 3, 9, 0))    # Saturday the 3rd: day-of-month matches
    assert c.matches(datetime(2026, 10, 12, 9, 0))   # Monday the 12th: weekday matches
    assert not c.matches(datetime(2026, 10, 13, 9, 0))  # Tuesday the 13th: neither


def test_a_star_in_either_day_field_means_only_the_other_restricts():
    assert parse("0 9 * * mon").matches(datetime(2026, 10, 12, 9)) \
        and not parse("0 9 * * mon").matches(datetime(2026, 10, 13, 9))
    only_first_week = parse("0 9 1-7 * *")
    assert only_first_week.matches(datetime(2026, 10, 6, 9)) and not only_first_week.matches(datetime(2026, 10, 8, 9))


def test_a_field_that_starts_with_a_star_counts_as_a_star():
    c = parse("0 9 */2 * mon")  # odd days AND Mondays
    assert c.dom_is_star
    assert c.matches(datetime(2026, 10, 5, 9))       # Monday the 5th
    assert not c.matches(datetime(2026, 10, 12, 9))  # Monday the 12th (even day)


# ------------------------------------------------------------- next_after


@pytest.mark.parametrize(
    "expression, after, expected",
    [
        ("0 2 * * *", utc(2026, 10, 24, 12), utc(2026, 10, 25, 2)),
        ("0 2 * * *", utc(2026, 10, 24, 1, 59, 59), utc(2026, 10, 24, 2)),
        ("0 2 * * *", utc(2026, 10, 24, 2, 0, 0), utc(2026, 10, 25, 2)),   # strictly AFTER
        ("0 10 * * *", utc(2026, 10, 24, 10, 0, 30), utc(2026, 10, 25, 10)),
        ("*/15 * * * *", utc(2026, 10, 24, 10, 7), utc(2026, 10, 24, 10, 15)),
        ("*/15 * * * *", utc(2026, 10, 24, 10, 45), utc(2026, 10, 24, 11, 0)),
        ("0 0 1 * *", utc(2026, 12, 15), utc(2027, 1, 1)),
        ("59 23 31 12 *", utc(2026, 12, 31, 23, 59), utc(2027, 12, 31, 23, 59)),
        ("30 3 * * sun", utc(2026, 10, 24, 12), utc(2026, 10, 25, 3, 30)),     # Saturday -> Sunday
        ("@weekly", utc(2026, 10, 26), utc(2026, 11, 1)),                       # Monday -> next Sunday
        ("0 9-17/4 * * mon-fri", utc(2026, 10, 23, 17), utc(2026, 10, 26, 9)),  # Friday 17:00 -> Monday
        ("0 0 29 2 *", utc(2026, 10, 24), utc(2028, 2, 29)),
        ("0 0 29 2 *", utc(2028, 2, 29), utc(2032, 2, 29)),
        ("0 0 29 2 *", utc(2096, 3, 1), utc(2104, 2, 29)),                      # 2100 is not a leap year
        ("0 0 31 * *", utc(2026, 2, 1), utc(2026, 3, 31)),                      # skips short months
        ("0 12 * jun *", utc(2026, 10, 24), utc(2027, 6, 1, 12)),
    ],
)
def test_next_after(expression, after, expected):
    assert parse(expression).next_after(after) == expected


def test_next_after_rejects_a_naive_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse("0 2 * * *").next_after(datetime(2026, 10, 24))


def test_next_runs_are_increasing_and_the_right_number():
    runs = parse("0 */6 * * *").next_runs(utc(2026, 10, 24, 1), UTC, 5)
    assert runs == [utc(2026, 10, 24, 6), utc(2026, 10, 24, 12), utc(2026, 10, 24, 18),
                    utc(2026, 10, 25, 0), utc(2026, 10, 25, 6)]


def test_result_is_in_the_requested_time_zone():
    run = parse("0 2 * * *").next_after(utc(2026, 10, 24, 12), BERLIN)
    assert run.utcoffset() == timedelta(hours=2) and (run.hour, run.tzname()) == (2, "CEST")
    assert run.astimezone(UTC) == utc(2026, 10, 25, 0)  # compare in UTC: 02:00 here is in the repeated hour


def test_a_zone_without_daylight_saving():
    kolkata = ZoneInfo("Asia/Kolkata")
    run = parse("0 2 * * *").next_after(utc(2026, 10, 24, 12), kolkata)
    assert run == utc(2026, 10, 24, 20, 30) and run.hour == 2


def brute_force_next(expression, after, window_minutes):
    """The obviously-correct way: look at every minute until one matches."""
    moment = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(window_minutes):
        if expression.matches(moment):
            return moment
        moment += timedelta(minutes=1)
    return None


def random_expression(rng):
    def field(low, high):
        kind = rng.choice(["*", "*", "step", "one", "range", "list"])
        if kind == "*":
            return "*"
        if kind == "step":
            return f"*/{rng.randint(1, max(1, (high - low) // 2 or 1))}"
        if kind == "one":
            return str(rng.randint(low, high))
        if kind == "range":
            a = rng.randint(low, high)
            return f"{a}-{rng.randint(a, high)}"
        return ",".join(str(rng.randint(low, high)) for _ in range(rng.randint(2, 3)))

    return " ".join([field(0, 59), field(0, 23), field(1, 31), field(1, 12), field(0, 6)])


def test_next_after_agrees_with_checking_every_single_minute():
    """400 random schedules, each compared with a minute-by-minute search."""
    window = 3 * 24 * 60
    checked = 0
    for seed in range(400):
        rng = random.Random(seed)
        text = random_expression(rng)
        try:
            expression = parse(text)
        except CronError:
            continue  # e.g. 'the 31st of February'
        after = utc(2026, 1, 1) + timedelta(minutes=rng.randint(0, 60 * 24 * 700), seconds=rng.randint(0, 59))
        expected = brute_force_next(expression, after, window)
        got = expression.next_after(after)
        if expected is not None:
            assert got == expected, f"seed {seed}: '{text}' after {after}"
            checked += 1
        else:
            assert got > after + timedelta(minutes=window - 1), f"seed {seed}: '{text}' after {after}"
        assert expression.matches(got), f"seed {seed}: '{text}' returned a time that does not match"
    assert checked > 100  # the comparison really did run on plenty of cases


# ------------------------------------------------------- daylight saving


def test_a_run_in_the_hour_that_is_skipped_happens_an_hour_later_that_day():
    """New York, 8 March 2026: clocks go from 02:00 straight to 03:00."""
    c = parse("30 2 * * *")
    day_before = c.next_after(datetime(2026, 3, 7, 1, 0, tzinfo=NY), NY)
    assert (day_before.day, day_before.hour, day_before.minute, day_before.tzname()) == (7, 2, 30, "EST")
    skipped = c.next_after(day_before, NY)
    assert (skipped.day, skipped.hour, skipped.minute, skipped.tzname()) == (8, 3, 30, "EDT")
    assert skipped.astimezone(UTC) == utc(2026, 3, 8, 7, 30)
    after = c.next_after(skipped, NY)
    assert (after.day, after.hour, after.minute) == (9, 2, 30)  # back to normal the next day


def test_a_run_in_the_hour_that_repeats_happens_once():
    """New York, 1 November 2026: 01:00-02:00 happens twice."""
    c = parse("30 1 * * *")
    first = c.next_after(datetime(2026, 10, 31, 12, tzinfo=NY), NY)
    assert first.astimezone(UTC) == utc(2026, 11, 1, 5, 30) and first.tzname() == "EDT"   # the first 01:30
    second = c.next_after(first, NY)
    assert second.astimezone(UTC) == utc(2026, 11, 2, 6, 30) and second.tzname() == "EST"  # NOT the repeated 01:30


def test_frequent_jobs_run_once_per_wall_clock_time_in_the_repeated_hour():
    c = parse("*/30 * * * *")
    at_first_0130 = utc(2026, 11, 1, 5, 30)
    assert c.next_after(at_first_0130, NY).astimezone(UTC) == utc(2026, 11, 1, 7, 0)  # 02:00 EST; the second 01:00/01:30 are skipped


def test_europe_changes_on_different_dates_than_america():
    c = parse("0 2 * * *")
    before = c.next_after(datetime(2026, 10, 24, 12, tzinfo=BERLIN), BERLIN)
    assert (before.day, before.tzname()) == (25, "CEST")  # 02:00 on the 25th happens before clocks go back at 03:00
    after = c.next_after(before, BERLIN)
    assert (after.day, after.hour, after.tzname()) == (26, 2, "CET")


def test_python_compares_times_in_a_repeated_hour_by_wall_clock_so_the_scheduler_uses_utc():
    """Why the scheduler keeps UTC internally: this is the Python behaviour (PEP 495) it avoids."""
    first = datetime(2026, 11, 1, 1, 30, tzinfo=NY)           # 01:30 EDT
    second = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NY)  # 01:30 EST, a real hour later
    assert first == second and not first < second              # same zone: compared as wall-clock readings
    assert first.astimezone(UTC) < second.astimezone(UTC)      # in UTC they are correctly ordered


def test_in_the_second_pass_through_the_repeated_hour_the_already_passed_time_is_not_returned():
    """It is 01:15 for the SECOND time on 1 Nov (EST). The next '01:30' must not be the
    first 01:30 (EDT), an hour ago: that is in the past."""
    second_pass_0115 = utc(2026, 11, 1, 6, 15)  # 01:15 EST
    assert second_pass_0115.astimezone(NY).strftime("%H:%M %Z") == "01:15 EST"
    run = parse("30 1 * * *").next_after(second_pass_0115, NY)
    assert run.astimezone(UTC) == utc(2026, 11, 2, 6, 30)  # tomorrow's 01:30, not an hour ago
    assert run.astimezone(UTC) > second_pass_0115
