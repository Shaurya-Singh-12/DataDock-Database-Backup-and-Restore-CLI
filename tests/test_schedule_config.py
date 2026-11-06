"""Reading and checking the `schedule:` section. Mistakes must stop the scheduler
when it starts, not at 2 a.m."""

import re
from datetime import timedelta

import pytest

from yourtool.config import ConfigError, resolve_config
from yourtool.cron import CronError
from yourtool.schedule import OVERRIDABLE, ScheduleError, load_schedule

BASE = """\
database:
  type: postgres
  user: u
  name: demo
retention:
  keep_last: 3
schedule:
  timezone: Europe/Berlin
  misfire_grace_minutes: 30
  jobs:
    - name: nightly-backup
      command: backup
      cron: "0 2 * * *"
    - name: weekly-prune
      command: prune
      cron: "30 3 * * sun"
      delete_without_asking: true
    - name: daily-check
      command: verify
      cron: "@daily"
      scope: all
"""


def load(tmp_path, text=BASE):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_schedule(path, env={})


def fails(tmp_path, text, fragment):
    with pytest.raises(ScheduleError, match=re.escape(fragment)):
        load(tmp_path, text)


def with_jobs(jobs_yaml, header="database:\n  user: u\n  name: demo\nretention:\n  keep_last: 3\n", extra=""):
    return f"{header}schedule:\n{extra}  jobs:\n{jobs_yaml}"


# ---------------------------------------------------------------- loading


def test_a_valid_schedule_loads(tmp_path):
    schedule = load(tmp_path)
    assert schedule.timezone_name == "Europe/Berlin"
    assert schedule.misfire_grace == timedelta(minutes=30)
    assert [(j.name, j.command) for j in schedule.jobs] == [
        ("nightly-backup", "backup"), ("weekly-prune", "prune"), ("daily-check", "verify")]
    assert schedule.jobs[0].cron.text == "0 2 * * *"
    assert schedule.jobs[1].delete_without_asking is True
    assert schedule.jobs[2].scope == "all" and schedule.jobs[0].scope == "latest"


def test_defaults_are_utc_and_an_hour_of_grace(tmp_path):
    schedule = load(tmp_path, with_jobs('    - {name: a, command: backup, cron: "0 2 * * *"}\n'))
    assert schedule.timezone_name == "UTC" and schedule.misfire_grace == timedelta(minutes=60)


def test_each_job_gets_its_own_resolved_settings(tmp_path):
    schedule = load(tmp_path)
    backup, prune, verify = schedule.jobs
    assert backup.config.db_name == "demo" and backup.config.db_user == "u"
    assert prune.config.keep_last == 3  # from the shared retention: section
    assert verify.config.db_name == "demo"


def test_a_job_can_override_settings_without_affecting_the_others(tmp_path):
    jobs = """\
    - {name: demo-backup, command: backup, cron: "0 2 * * *"}
    - name: shop-backup
      command: backup
      cron: "0 3 * * *"
      with: {db_name: shop, compression: gzip}
    - name: tight-prune
      command: prune
      cron: "0 4 * * *"
      delete_without_asking: true
      with: {keep_last: 9, keep_monthly: 2}
"""
    demo, shop, prune = load(tmp_path, with_jobs(jobs)).jobs
    assert (demo.config.db_name, demo.config.compression) == ("demo", "none")
    assert (shop.config.db_name, shop.config.compression) == ("shop", "gzip")
    assert (prune.config.keep_last, prune.config.keep_monthly) == (9, 2)  # beats the file's 3


def test_verify_jobs_need_no_database_settings(tmp_path):
    text = "schedule:\n  jobs:\n    - {name: check, command: verify, cron: '@daily'}\n"
    assert load(tmp_path, text).jobs[0].command == "verify"


def test_the_schedule_section_does_not_disturb_normal_settings_loading(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE, encoding="utf-8")
    config, _ = resolve_config(config_file=path, env={})  # no "unknown section 'schedule'"
    assert config.db_name == "demo"


def test_job_lookup_by_name(tmp_path):
    schedule = load(tmp_path)
    assert schedule.job("weekly-prune").command == "prune"
    with pytest.raises(ScheduleError, match="no job called 'nope'.*nightly-backup"):
        schedule.job("nope")


def test_passwords_and_log_files_cannot_be_overridden_per_job():
    assert "db_password" not in OVERRIDABLE and "log_file" not in OVERRIDABLE
    assert "db_name" in OVERRIDABLE and "compression" in OVERRIDABLE


# ---------------------------------------------------------------- the file


def test_missing_file(tmp_path):
    with pytest.raises(ScheduleError, match="Cannot read the config file"):
        load_schedule(tmp_path / "nope.yaml", env={})


def test_invalid_yaml(tmp_path):
    fails(tmp_path, "schedule: [unclosed", "Invalid YAML")


@pytest.mark.parametrize("text", ["", "database:\n  user: u\n", "schedule: []\n", "schedule: nothing\n"])
def test_no_schedule_section(tmp_path, text):
    fails(tmp_path, text, "has no 'schedule:' section")


def test_unknown_setting_in_the_schedule_section(tmp_path):
    fails(tmp_path, with_jobs('    - {name: a, command: verify, cron: "@daily"}\n', extra="  timeozne: UTC\n"),
          "unknown setting 'timeozne'")


@pytest.mark.parametrize("jobs", ["", "    []\n"])
def test_needs_at_least_one_job(tmp_path, jobs):
    fails(tmp_path, with_jobs(jobs) if jobs else "schedule:\n  timezone: UTC\n", "needs a 'jobs:' list")


@pytest.mark.parametrize("bad", ["Mars/Base", "Europe/Atlantis", "nonsense"])
def test_unknown_time_zone(tmp_path, bad):
    fails(tmp_path, with_jobs('    - {name: a, command: verify, cron: "@daily"}\n', extra=f"  timezone: {bad}\n"),
          f"Unknown time zone '{bad}'")


def test_time_zone_must_be_text(tmp_path):
    fails(tmp_path, with_jobs('    - {name: a, command: verify, cron: "@daily"}\n', extra="  timezone: 5\n"),
          "'timezone' must be a name")


@pytest.mark.parametrize("value", ["0", "1441", "-5", "abc", "true", "1.5"])
def test_grace_period_must_be_a_sensible_whole_number(tmp_path, value):
    fails(tmp_path, with_jobs('    - {name: a, command: verify, cron: "@daily"}\n',
                              extra=f"  misfire_grace_minutes: {value}\n"),
          "'misfire_grace_minutes' must be a whole number from 1 to 1440")


# ----------------------------------------------------------------- the jobs


def job(body, **kw):
    return with_jobs(f"    - {body}\n", **kw)


def test_a_job_must_be_a_mapping(tmp_path):
    fails(tmp_path, with_jobs("    - just a string\n"), "Job #1 must be a mapping")


@pytest.mark.parametrize("name", ["", "has space", "-leading-dash", "x" * 65, "semi;colon"])
def test_job_names_are_checked(tmp_path, name):
    fails(tmp_path, job(f'{{name: "{name}", command: verify, cron: "@daily"}}'), "needs a 'name'")


def test_a_job_without_a_name(tmp_path):
    fails(tmp_path, job('{command: verify, cron: "@daily"}'), "Job #1 needs a 'name'")


def test_duplicate_job_names(tmp_path):
    jobs = '    - {name: a, command: verify, cron: "@daily"}\n    - {name: a, command: verify, cron: "@hourly"}\n'
    fails(tmp_path, with_jobs(jobs), "Two jobs are called 'a'")


def test_unknown_job_setting_is_caught_as_a_typo(tmp_path):
    fails(tmp_path, job('{name: a, command: verify, cron: "@daily", scheduel: x}'),
          "Job 'a' has an unknown setting 'scheduel'")


@pytest.mark.parametrize("command", ["restore", "list", "", "BACKUP"])
def test_command_must_be_one_of_the_three(tmp_path, command):
    fails(tmp_path, job(f'{{name: a, command: "{command}", cron: "@daily"}}'),
          "'command' must be one of backup, prune, verify")


def test_cron_is_required_and_must_be_text(tmp_path):
    fails(tmp_path, job("{name: a, command: verify}"), "'cron' is required")
    fails(tmp_path, job("{name: a, command: verify, cron: 5}"), "'cron' is required")


def test_a_bad_cron_names_the_job_and_the_problem(tmp_path):
    with pytest.raises(ScheduleError) as caught:
        load(tmp_path, job('{name: a, command: verify, cron: "61 * * * *"}'))
    message = str(caught.value)
    assert "Job 'a'" in message and "61 * * * *" in message and "out of range" in message


def test_cron_problems_are_also_configuration_errors(tmp_path):
    with pytest.raises(ConfigError):
        load(tmp_path, job('{name: a, command: verify, cron: "nonsense"}'))
    assert issubclass(CronError, ConfigError)


# ------------------------------------------------- the prune safety switch


@pytest.mark.parametrize("flag", ["", ", delete_without_asking: false", ', delete_without_asking: "true"',
                                  ", delete_without_asking: 1", ", delete_without_asking: null"])
def test_a_prune_job_must_explicitly_allow_deleting_without_asking(tmp_path, flag):
    with pytest.raises(ScheduleError) as caught:
        load(tmp_path, job(f'{{name: p, command: prune, cron: "0 3 * * 0"{flag}}}'))
    message = str(caught.value)
    assert "Job 'p' would delete old backups with nobody there to confirm" in message
    assert "delete_without_asking: true" in message


def test_the_switch_is_accepted_when_it_is_really_true(tmp_path):
    schedule = load(tmp_path, job('{name: p, command: prune, cron: "0 3 * * 0", delete_without_asking: true}'))
    assert schedule.jobs[0].delete_without_asking is True


def test_the_switch_means_nothing_on_other_jobs(tmp_path):
    fails(tmp_path, job('{name: a, command: backup, cron: "@daily", delete_without_asking: true}'),
          "'delete_without_asking' only applies to prune jobs")


def test_a_prune_job_needs_retention_rules(tmp_path):
    text = with_jobs('    - {name: p, command: prune, cron: "0 3 * * 0", delete_without_asking: true}\n',
                     header="database:\n  user: u\n  name: demo\n")
    fails(tmp_path, text, "Job 'p' (prune) has no retention rules")


def test_a_prune_job_can_bring_its_own_rules(tmp_path):
    text = with_jobs("    - {name: p, command: prune, cron: '0 3 * * 0', delete_without_asking: true, with: {keep_last: 5}}\n",
                     header="database:\n  user: u\n  name: demo\n")
    assert load(tmp_path, text).jobs[0].config.keep_last == 5


def test_a_prune_job_needs_to_know_which_database(tmp_path):
    text = with_jobs("    - {name: p, command: prune, cron: '0 3 * * 0', delete_without_asking: true}\n",
                     header="retention:\n  keep_last: 3\n")
    fails(tmp_path, text, "db_name")


# ------------------------------------------------------ scope and `with`


def test_scope_only_for_verify_and_only_two_values(tmp_path):
    fails(tmp_path, job('{name: a, command: backup, cron: "@daily", scope: all}'),
          "'scope' only applies to verify jobs")
    fails(tmp_path, job('{name: a, command: verify, cron: "@daily", scope: some}'),
          "'scope' must be 'latest' or 'all'")


def test_with_must_be_a_mapping(tmp_path):
    fails(tmp_path, job('{name: a, command: backup, cron: "@daily", with: [db_name, shop]}'),
          "'with' must be a mapping")


def test_unknown_override_lists_what_is_allowed(tmp_path):
    with pytest.raises(ScheduleError) as caught:
        load(tmp_path, job('{name: a, command: backup, cron: "@daily", with: {bogus: 1}}'))
    assert "unknown setting 'bogus'" in str(caught.value) and "db_name" in str(caught.value)


def test_a_password_cannot_be_put_in_a_job(tmp_path):
    fails(tmp_path, job('{name: a, command: backup, cron: "@daily", with: {db_password: hunter2}}'),
          "passwords cannot be set per job")


def test_override_values_must_be_single_values(tmp_path):
    fails(tmp_path, job('{name: a, command: backup, cron: "@daily", with: {db_name: [a, b]}}'),
          "must be a single value")


def test_a_bad_override_value_names_the_job(tmp_path):
    with pytest.raises(ScheduleError) as caught:
        load(tmp_path, job('{name: p, command: prune, cron: "@daily", delete_without_asking: true, with: {keep_daily: 0}}'))
    assert "Job 'p' (prune)" in str(caught.value) and "whole number of 1 or more" in str(caught.value)


def test_a_backup_job_needs_a_database_user(tmp_path):
    text = with_jobs('    - {name: a, command: backup, cron: "@daily"}\n', header="database:\n  name: demo\n")
    fails(tmp_path, text, "Job 'a' (backup)")
    with pytest.raises(ScheduleError, match="db_user"):
        load(tmp_path, text)
