"""The schedule: which jobs run, and when. Read from the `schedule:` section of
the config file.

    schedule:
      timezone: Europe/Berlin          # default UTC
      misfire_grace_minutes: 60        # how late a run may start (default 60)
      jobs:
        - name: nightly-backup
          command: backup              # backup | prune | verify
          cron: "0 2 * * *"
        - name: weekly-prune
          command: prune
          cron: "30 3 * * sun"
          delete_without_asking: true  # REQUIRED for prune: nobody is there to confirm
          with: {keep_last: 7, keep_weekly: 4}
        - name: daily-check
          command: verify
          cron: "0 4 * * *"
          scope: latest                # or: all
        - name: shop-backup            # a second database: just override its settings
          command: backup
          cron: "0 3 * * *"
          with: {db_name: shop}

Everything is checked when the file is loaded, so a mistake stops the scheduler
at startup instead of failing at 2 a.m.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import timedelta, tzinfo
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from yourtool.config import Config, ConfigError, resolve_config
from yourtool.cron import CronError, CronExpression
from yourtool.retention import Policy

COMMANDS = ("backup", "prune", "verify")
DEFAULT_GRACE_MINUTES = 60

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SCHEDULE_KEYS = {"timezone", "misfire_grace_minutes", "jobs"}
_JOB_KEYS = {"name", "command", "cron", "delete_without_asking", "scope", "with"}

# Secrets and the log file are never set per job.
_NOT_PER_JOB = {"db_password", "log_file"}
OVERRIDABLE = tuple(sorted({f.name for f in fields(Config)} - _NOT_PER_JOB))

# What each kind of job needs to know before it can run.
_REQUIRED = {"backup": ("db_user", "db_name"), "prune": ("db_name",), "verify": ()}


class ScheduleError(ConfigError):
    """The schedule in the config file is not valid."""


@dataclass(frozen=True)
class Job:
    name: str
    command: str
    cron: CronExpression
    config: Config  # this job's own, fully resolved settings
    delete_without_asking: bool = False
    scope: str = "latest"  # verify jobs: "latest" or "all"


@dataclass(frozen=True)
class Schedule:
    timezone_name: str
    tz: tzinfo
    misfire_grace: timedelta
    jobs: tuple[Job, ...]

    def job(self, name: str) -> Job:
        for job in self.jobs:
            if job.name == name:
                return job
        known = ", ".join(j.name for j in self.jobs)
        raise ScheduleError(f"There is no job called '{name}'. Jobs in this schedule: {known}.")


def _read_section(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScheduleError(f"Cannot read the config file {path}: {exc}") from None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ScheduleError(f"Invalid YAML in {path}: {exc}") from None
    section = data.get("schedule") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        raise ScheduleError(
            f"{path} has no 'schedule:' section. Add one with a 'jobs:' list "
            "(see the README, 'Scheduling')."
        )
    return section


def _check_keys(where: str, given: Mapping[str, Any], allowed: set[str]) -> None:
    for key in given:
        if key not in allowed:
            raise ScheduleError(f"{where} has an unknown setting '{key}'. Allowed: {', '.join(sorted(allowed))}.")


def _timezone(name: Any) -> tuple[str, tzinfo]:
    if not isinstance(name, str) or not name:
        raise ScheduleError("'timezone' must be a name such as UTC or Europe/Berlin.")
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError(
            f"Unknown time zone '{name}'. Use an IANA name such as UTC, Europe/Berlin or America/New_York."
        ) from None


def _grace(value: Any) -> timedelta:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
        raise ScheduleError("'misfire_grace_minutes' must be a whole number from 1 to 1440.")
    return timedelta(minutes=value)


def _job(
    index: int, raw: Any, config_file: Path, env: Mapping[str, str] | None, seen: set[str]
) -> Job:
    if not isinstance(raw, dict):
        raise ScheduleError(f"Job #{index} must be a mapping with name, command and cron.")
    name = raw.get("name")
    where = f"Job #{index}" if not name else f"Job '{name}'"
    _check_keys(where, raw, _JOB_KEYS)

    if not isinstance(name, str) or not _NAME.match(name):
        raise ScheduleError(
            f"Job #{index} needs a 'name' of letters, digits, '.', '_' or '-' (up to 64 characters)."
        )
    if name in seen:
        raise ScheduleError(f"Two jobs are called '{name}'. Job names must be unique.")
    seen.add(name)

    command = raw.get("command")
    if command not in COMMANDS:
        raise ScheduleError(f"{where}: 'command' must be one of {', '.join(COMMANDS)}, got {command!r}.")

    cron_text = raw.get("cron")
    if not isinstance(cron_text, str):
        raise ScheduleError(f"{where}: 'cron' is required, for example cron: \"0 2 * * *\" (quote it).")
    try:
        cron = CronExpression.parse(cron_text)
    except CronError as exc:
        raise ScheduleError(f"{where}: invalid cron schedule '{cron_text}'. {exc}") from None

    delete_ok = raw.get("delete_without_asking", False)
    if command == "prune":
        if delete_ok is not True:
            raise ScheduleError(
                f"{where} would delete old backups with nobody there to confirm. If that is what "
                "you want, add  delete_without_asking: true  to this job. (To delete by hand "
                "instead, run 'yourtool prune' yourself; it asks first.)"
            )
    elif "delete_without_asking" in raw:
        raise ScheduleError(f"{where}: 'delete_without_asking' only applies to prune jobs.")

    scope = raw.get("scope", "latest")
    if command == "verify":
        if scope not in ("latest", "all"):
            raise ScheduleError(f"{where}: 'scope' must be 'latest' or 'all', got {scope!r}.")
    elif "scope" in raw:
        raise ScheduleError(f"{where}: 'scope' only applies to verify jobs.")

    overrides = raw.get("with", {}) or {}
    if not isinstance(overrides, dict):
        raise ScheduleError(f"{where}: 'with' must be a mapping, for example with: {{db_name: shop}}.")
    for key, value in overrides.items():
        if key == "db_password":
            raise ScheduleError(
                f"{where}: passwords cannot be set per job. Use the YOURTOOL_DB_PASSWORD "
                "environment variable (or the database's own password mechanism)."
            )
        if key not in OVERRIDABLE:
            raise ScheduleError(
                f"{where}: 'with' has an unknown setting '{key}'. Allowed: {', '.join(OVERRIDABLE)}."
            )
        if isinstance(value, (dict, list)):
            raise ScheduleError(f"{where}: the value of '{key}' must be a single value.")

    try:
        config, _ = resolve_config(
            cli=overrides, config_file=config_file, env=env, required=_REQUIRED[command]
        )
    except ConfigError as exc:
        raise ScheduleError(f"{where} ({command}): {exc}") from None

    if command == "prune":
        policy = Policy(config.keep_last, config.keep_daily, config.keep_weekly, config.keep_monthly)
        if policy.is_empty():
            raise ScheduleError(
                f"{where} (prune) has no retention rules. Add them under 'retention:' in the config "
                "file, or on the job, for example with: {keep_last: 7}."
            )

    return Job(
        name=name, command=command, cron=cron, config=config,
        delete_without_asking=delete_ok is True, scope=scope if command == "verify" else "latest",
    )


def load_schedule(config_file: Path, env: Mapping[str, str] | None = None) -> Schedule:
    """Read and fully validate the schedule. Raises ScheduleError with a clear message."""
    path = Path(config_file)
    section = _read_section(path)
    _check_keys("The 'schedule' section", section, _SCHEDULE_KEYS)

    name, tz = _timezone(section.get("timezone", "UTC"))
    grace = _grace(section.get("misfire_grace_minutes", DEFAULT_GRACE_MINUTES))

    raw_jobs = section.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ScheduleError("The 'schedule' section needs a 'jobs:' list with at least one job.")
    seen: set[str] = set()
    jobs = tuple(_job(i, raw, path, env, seen) for i, raw in enumerate(raw_jobs, start=1))
    return Schedule(timezone_name=name, tz=tz, misfire_grace=grace, jobs=jobs)
