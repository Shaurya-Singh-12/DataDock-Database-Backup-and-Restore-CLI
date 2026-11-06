"""The scheduler loop, driven by a fake clock so days of schedule run instantly."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from yourtool.config import Config
from yourtool.cron import CronExpression
from yourtool.jobs import JobOutcome
from yourtool.schedule import Job, Schedule
from yourtool.scheduler import MAX_SLEEP_SECONDS, Scheduler

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
CONFIG = Config(db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="d")


def utc(*args):
    return datetime(*args, tzinfo=UTC)


class Harness:
    """A pretend clock. Waiting simply moves it forward."""

    def __init__(self, start):
        self.now = start
        self.waits = []
        self.runs = []  # (job name, time it started)

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        return False

    def runner(self, seconds=None, failing=(), crashing=()):
        seconds = seconds or {}

        def run(job):
            self.runs.append((job.name, self.now))
            self.now += timedelta(seconds=seconds.get(job.name, 1))
            if job.name in crashing:
                raise RuntimeError("boom")
            return JobOutcome(job.name not in failing, "done" if job.name not in failing else "it broke")

        return run


def schedule(*specs, tz=UTC, grace=60):
    jobs = tuple(Job(name=n, command="backup", cron=CronExpression.parse(c), config=CONFIG) for n, c in specs)
    return Schedule(timezone_name=getattr(tz, "key", "UTC"), tz=tz, misfire_grace=timedelta(minutes=grace), jobs=jobs)


def run_until(h, sched, end, runner=None, **kwargs):
    scheduler = Scheduler(sched, runner or h.runner(), clock=h.clock, wait=h.wait, **kwargs)
    scheduler.run(lambda: h.now >= end)
    return scheduler


# ---------------------------------------------------------------- timing


def test_a_daily_job_runs_at_its_time_each_day():
    h = Harness(utc(2026, 10, 24, 0, 0))
    run_until(h, schedule(("nightly", "0 2 * * *")), utc(2026, 10, 27))
    assert [t for _, t in h.runs] == [utc(2026, 10, 24, 2), utc(2026, 10, 25, 2), utc(2026, 10, 26, 2)]


def test_starting_exactly_at_the_scheduled_minute_waits_for_the_next_one():
    h = Harness(utc(2026, 10, 24, 2, 0, 0))
    run_until(h, schedule(("nightly", "0 2 * * *")), utc(2026, 10, 26))
    assert [t for _, t in h.runs] == [utc(2026, 10, 25, 2)]


def test_jobs_due_at_the_same_time_run_one_after_another_never_together():
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("b-job", "0 2 * * *"), ("a-job", "0 2 * * *")), utc(2026, 10, 24, 3),
              runner=h.runner(seconds={"a-job": 90}))
    assert h.runs == [("a-job", utc(2026, 10, 24, 2, 0, 0)), ("b-job", utc(2026, 10, 24, 2, 1, 30))]


def test_sleeps_are_short_so_a_waking_laptop_is_noticed():
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("nightly", "0 10 * * *")), utc(2026, 10, 24, 11))  # 10 hours of waiting
    assert max(h.waits) == MAX_SLEEP_SECONDS == 30.0
    assert all(0 < w <= 30 for w in h.waits)
    assert len(h.waits) >= 10 * 3600 // 30


def test_the_run_starts_on_time_not_early_or_late():
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("j", "*/10 * * * *")), utc(2026, 10, 24, 1))
    assert [t.minute for _, t in h.runs] == [10, 20, 30, 40, 50]
    assert all(t.second == 0 for _, t in h.runs)


# ------------------------------------------------- long jobs and missed runs


def test_a_job_that_became_due_while_another_ran_starts_as_soon_as_the_scheduler_is_free(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("long", "0 2 * * *"), ("other", "0 3 * * *")), utc(2026, 10, 24, 6),
              runner=h.runner(seconds={"long": 90 * 60}))
    assert h.runs == [("long", utc(2026, 10, 24, 2)), ("other", utc(2026, 10, 24, 3, 30, 0))]  # due 03:00, ran 03:30
    assert "1800 s late" in caplog.text


def test_a_run_later_than_the_grace_period_is_skipped_and_the_log_says_so(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("long", "0 2 24 10 *"), ("other", "0 3 * * *"), grace=10), utc(2026, 10, 25, 4),
              runner=h.runner(seconds={"long": 90 * 60}))  # "long" runs once, on 24 October
    others = [t for name, t in h.runs if name == "other"]
    assert others == [utc(2026, 10, 25, 3)]  # the 24th's run was skipped; the 25th's ran on time
    assert "MISSED its 2026-10-24 03:00 UTC run" in caplog.text and "the limit is 10" in caplog.text


def test_after_a_long_job_there_is_no_burst_of_catch_up_runs():
    h = Harness(utc(2026, 10, 24, 2, 0, 0))
    run_until(h, schedule(("minutely", "* * * * *")), utc(2026, 10, 24, 2, 16),
              runner=h.runner(seconds={"minutely": 300}))  # each run takes 5 minutes
    assert [t for _, t in h.runs] == [utc(2026, 10, 24, 2, 1), utc(2026, 10, 24, 2, 7), utc(2026, 10, 24, 2, 13)]


def test_a_computer_that_slept_through_the_run_skips_it(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 1, 0))
    jumped = []

    def sleepy_wait(seconds):  # the laptop sleeps for five hours during the first wait
        if not jumped:
            jumped.append(True)
            h.now += timedelta(hours=5)
            return False
        return h.wait(seconds)

    scheduler = Scheduler(schedule(("nightly", "0 2 * * *")), h.runner(), clock=h.clock, wait=sleepy_wait)
    scheduler.run(lambda: h.now >= utc(2026, 10, 26, 0))
    assert [t for _, t in h.runs] == [utc(2026, 10, 25, 2)]  # the 24th's 02:00 was slept through, not run at 06:00
    assert "MISSED" in caplog.text


def test_waking_up_shortly_after_the_run_time_still_runs_it():
    h = Harness(utc(2026, 10, 24, 1, 0))
    jumped = []

    def short_nap(seconds):
        if not jumped:
            jumped.append(True)
            h.now = utc(2026, 10, 24, 2, 20)  # woke 20 minutes after it was due; the grace is 60
            return False
        return h.wait(seconds)

    Scheduler(schedule(("nightly", "0 2 * * *")), h.runner(), clock=h.clock, wait=short_nap).run(
        lambda: h.now >= utc(2026, 10, 24, 5))
    assert h.runs == [("nightly", utc(2026, 10, 24, 2, 20))]


# --------------------------------------------------------------- failures


def test_a_failing_job_is_logged_and_the_scheduler_carries_on(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 0))
    results = []
    run_until(h, schedule(("flaky", "0 2 * * *")), utc(2026, 10, 26, 12), runner=h.runner(failing={"flaky"}),
              on_result=results.append)
    assert len(h.runs) == 3  # the 24th, 25th and 26th: it did not give up after the first failure
    assert all(not r.outcome.ok and r.outcome.message == "it broke" for r in results)
    assert "Job 'flaky' FAILED" in caplog.text


def test_a_job_that_crashes_does_not_stop_the_scheduler(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 0))
    results = []
    run_until(h, schedule(("buggy", "0 2 * * *")), utc(2026, 10, 26, 12), runner=h.runner(crashing={"buggy"}),
              on_result=results.append)
    assert len(h.runs) == 3
    assert results[0].outcome.ok is False
    assert "unexpected error (RuntimeError): boom" in results[0].outcome.message
    assert "Traceback" in caplog.text  # the details are in the log for the person to find


def test_one_failing_job_does_not_affect_another():
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("bad", "0 2 * * *"), ("good", "0 3 * * *")), utc(2026, 10, 24, 5),
              runner=h.runner(failing={"bad"}))
    assert [n for n, _ in h.runs] == ["bad", "good"]


# ------------------------------------------------------ stopping and results


def test_stop_request_ends_the_loop_promptly():
    h = Harness(utc(2026, 10, 24, 0))
    stop_after = utc(2026, 10, 24, 0, 10)
    scheduler = Scheduler(schedule(("nightly", "0 2 * * *")), h.runner(), clock=h.clock, wait=h.wait)
    assert scheduler.run(lambda: h.now >= stop_after) == 0
    assert h.now <= stop_after + timedelta(seconds=30)  # stopped within one sleep


def test_wait_returning_true_means_stop():
    h = Harness(utc(2026, 10, 24, 0))
    scheduler = Scheduler(schedule(("nightly", "0 2 * * *")), h.runner(), clock=h.clock, wait=lambda s: True)
    assert scheduler.run(lambda: False) == 0


def test_max_runs():
    h = Harness(utc(2026, 10, 24, 0))
    scheduler = Scheduler(schedule(("minutely", "* * * * *")), h.runner(), clock=h.clock, wait=h.wait)
    assert scheduler.run(lambda: False, max_runs=3) == 3 and len(h.runs) == 3


def test_results_report_when_it_was_due_started_and_finished():
    h = Harness(utc(2026, 10, 24, 0))
    results = []
    run_until(h, schedule(("nightly", "0 2 * * *")), utc(2026, 10, 24, 3), runner=h.runner(seconds={"nightly": 42}),
              on_result=results.append)
    (r,) = results
    assert r.job.name == "nightly" and r.outcome.ok
    assert r.scheduled == r.started == utc(2026, 10, 24, 2)
    assert r.finished == utc(2026, 10, 24, 2, 0, 42)


def test_the_log_tells_the_whole_story(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    h = Harness(utc(2026, 10, 24, 0))
    run_until(h, schedule(("nightly", "0 2 * * *")), utc(2026, 10, 24, 3))
    text = caplog.text
    assert "Job 'nightly' (backup) first runs at 2026-10-24 02:00 UTC" in text
    assert "Starting job 'nightly' (backup), scheduled for 2026-10-24 02:00 UTC" in text
    assert "Job 'nightly' finished OK in 1.0 s: done" in text
    assert "Job 'nightly' next runs at 2026-10-25 02:00 UTC" in text


# ------------------------------------------------------- daylight saving


def test_daily_job_across_the_spring_clock_change():
    h = Harness(utc(2026, 3, 6, 12))
    run_until(h, schedule(("j", "30 2 * * *"), tz=NY), utc(2026, 3, 10))
    assert [t for _, t in h.runs] == [utc(2026, 3, 7, 7, 30), utc(2026, 3, 8, 7, 30), utc(2026, 3, 9, 6, 30)]
    # 8 March, 02:30 does not exist in New York: the run happens at 03:30 EDT, the same instant as 07:30 UTC


def test_daily_job_across_the_autumn_clock_change_runs_once_that_night():
    h = Harness(utc(2026, 10, 30, 12))
    run_until(h, schedule(("j", "30 1 * * *"), tz=NY), utc(2026, 11, 3))
    assert [t for _, t in h.runs] == [utc(2026, 10, 31, 5, 30), utc(2026, 11, 1, 5, 30), utc(2026, 11, 2, 6, 30)]
    assert utc(2026, 11, 1, 6, 30) not in [t for _, t in h.runs]  # the repeated 01:30 does not run again


def test_times_are_kept_in_utc_inside_the_scheduler():
    """Same-zone datetimes compare by wall clock in Python, which mis-orders the repeated hour."""
    h = Harness(utc(2026, 10, 30, 12))
    scheduler = Scheduler(schedule(("a", "30 1 * * *"), ("b", "0 2 * * *"), tz=NY), h.runner(), clock=h.clock, wait=h.wait)
    scheduler.run(lambda: True)  # just set up
    times = scheduler.next_times()
    assert times == {} or all(t.tzinfo is UTC for t in times.values())
    scheduler.run(lambda: h.now >= utc(2026, 10, 30, 13), max_runs=1)
    assert all(t.tzinfo is UTC for t in scheduler.next_times().values())
