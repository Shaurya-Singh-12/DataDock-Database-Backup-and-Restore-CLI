"""The scheduler loop.

Rules it follows:

* One job at a time. Jobs never overlap, so two backups can't collide.
* A job that is due while another is running starts as soon as the scheduler
  is free, as long as it is not later than the grace period; otherwise that
  run is skipped (and logged), never silently dropped.
* No catching up: after a run, the next run is the next scheduled time AFTER
  now. A computer that slept through eight nightly backups does not run eight.
* A failing job is logged and the scheduler carries on.
* Sleeping is done in short pieces (at most 30 s), so a laptop waking from sleep
  or a changed system clock is noticed quickly.

The clock and the waiting are passed in, so tests can run weeks of schedule in
a blink with a fake clock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from yourtool.jobs import JobOutcome
from yourtool.schedule import Job, Schedule

log = logging.getLogger(__name__)

MAX_SLEEP_SECONDS = 30.0


@dataclass(frozen=True)
class JobRun:
    job: Job
    scheduled: datetime
    started: datetime
    finished: datetime
    outcome: JobOutcome


class Scheduler:
    def __init__(
        self,
        schedule: Schedule,
        run_job: Callable[[Job], JobOutcome],
        *,
        clock: Callable[[], datetime] | None = None,
        wait: Callable[[float], bool] | None = None,
        on_result: Callable[[JobRun], None] | None = None,
    ) -> None:
        """``wait(seconds)`` sleeps and returns True if the scheduler should stop."""
        self.schedule = schedule
        self._run_job = run_job
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._wait = wait or (lambda seconds: False)
        self._on_result = on_result
        self._next: dict[str, datetime] = {}

    def _next_run(self, job: Job, after: datetime) -> datetime:
        """When the job runs next, as a UTC time.

        Times are kept in UTC inside the scheduler and only shown in the job's zone.
        Python compares two times in the SAME zone by their wall-clock reading, so during
        the hour that repeats when clocks go back, two moments an hour apart would
        look equal (and sort wrongly). UTC has no such repeated hour.
        """
        return job.cron.next_after(after, self.schedule.tz).astimezone(timezone.utc)

    def _stamp(self, moment: datetime) -> str:
        return moment.astimezone(self.schedule.tz).strftime("%Y-%m-%d %H:%M %Z")

    def next_times(self) -> dict[str, datetime]:
        return dict(self._next)

    def run(self, stop_requested: Callable[[], bool], max_runs: int | None = None) -> int:
        """Run until stopped (or ``max_runs`` job runs). Returns how many jobs ran."""
        jobs = {job.name: job for job in self.schedule.jobs}
        now = self._clock()
        self._next = {name: self._next_run(job, now) for name, job in jobs.items()}
        for name, when in self._next.items():
            log.info("Job '%s' (%s) first runs at %s", name, jobs[name].command, self._stamp(when))

        runs = 0
        while not stop_requested():
            now = self._clock()
            due = sorted((when, name) for name, when in self._next.items() if when <= now)
            if not due:
                seconds = max((min(self._next.values()) - now).total_seconds(), 0.0)
                if self._wait(min(seconds, MAX_SLEEP_SECONDS)):
                    break
                continue

            scheduled, name = due[0]
            job = jobs[name]
            late = now - scheduled
            if late > self.schedule.misfire_grace:
                minutes = int(late.total_seconds() // 60)
                limit = int(self.schedule.misfire_grace.total_seconds() // 60)
                log.warning(
                    "Job '%s' MISSED its %s run: it was due %d minutes ago (the limit is %d). "
                    "Skipping that run.", name, self._stamp(scheduled), minutes, limit,
                )
                self._next[name] = self._next_run(job, now)
                log.info("Job '%s' next runs at %s", name, self._stamp(self._next[name]))
                continue

            note = f" ({int(late.total_seconds())} s late)" if late >= timedelta(seconds=60) else ""
            log.info("Starting job '%s' (%s), scheduled for %s%s",
                     name, job.command, self._stamp(scheduled), note)
            started = now
            try:
                outcome = self._run_job(job)
            except Exception as exc:  # the runner should not raise, but never let it stop us
                log.exception("Job '%s' crashed", name)
                outcome = JobOutcome(False, f"unexpected error ({type(exc).__name__}): {exc}")
            finished = self._clock()
            seconds = (finished - started).total_seconds()
            if outcome.ok:
                log.info("Job '%s' finished OK in %.1f s: %s", name, seconds, outcome.message)
            else:
                log.error("Job '%s' FAILED after %.1f s: %s", name, seconds, outcome.message)

            runs += 1
            self._next[name] = self._next_run(job, max(finished, scheduled))
            log.info("Job '%s' next runs at %s", name, self._stamp(self._next[name]))
            if self._on_result:
                self._on_result(JobRun(job, scheduled, started, finished, outcome))
            if max_runs is not None and runs >= max_runs:
                break
        return runs
