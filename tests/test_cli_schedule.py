"""`yourtool schedule list | run-now | run`."""

import signal
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeAdapter, FakeStorage, with_checksums
from typer.testing import CliRunner

import yourtool.cli as cli
import yourtool.jobs as jobs_module
from yourtool.jobs import JobOutcome

runner = CliRunner()
UTC = timezone.utc
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # a Saturday, 14:00 in Berlin

YAML = """\
database:
  type: postgres
  user: u
  name: demo
retention:
  keep_last: 2
schedule:
  timezone: Europe/Berlin
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
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def reset_stop_flag():
    cli._STOP.clear()
    yield
    cli._STOP.clear()


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(cli, "_clock", lambda: NOW)


def run(args, env=None):
    return runner.invoke(cli.app, args, env=env)


# ------------------------------------------------------------------ list


def test_list_shows_jobs_and_upcoming_times_in_both_zones(config_file, fixed_clock):
    result = run(["schedule", "list", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Time zone: Europe/Berlin" in out
    for name in ("nightly-backup", "weekly-prune", "daily-check"):
        assert name in out
    # 02:00 Berlin on 25 Oct is still summer time; by 26 Oct the clocks have gone back
    assert "next: Sun 2026-10-25 02:00 CEST   (00:00 UTC)" in out
    assert "next: Mon 2026-10-26 02:00 CET   (01:00 UTC)" in out
    assert out.count("next:") == 9  # three upcoming times for each of three jobs


def test_list_flags_the_job_that_deletes_without_asking(config_file, fixed_clock):
    out = run(["schedule", "list", "--config", str(config_file)]).stdout
    assert 'weekly-prune   prune (deletes without asking)' in out
    assert "nightly-backup   backup   cron" in out


def test_list_next_option(config_file, fixed_clock):
    out = run(["schedule", "list", "--config", str(config_file), "--next", "1"]).stdout
    assert out.count("next:") == 3


def test_list_runs_nothing(config_file, fixed_clock, monkeypatch):
    monkeypatch.setattr(cli, "execute_job", lambda job: pytest.fail("list must not run any job"))
    assert run(["schedule", "list", "--config", str(config_file)]).exit_code == 0


# ----------------------------------------- mistakes are caught up front


def test_a_config_file_is_required():
    result = run(["schedule", "list"])
    assert result.exit_code == 1 and "--config" in result.output


def test_a_file_without_a_schedule_section(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("database:\n  user: u\n  name: d\n")
    result = run(["schedule", "list", "--config", str(path)])
    assert result.exit_code == 1 and "no 'schedule:' section" in result.output


def test_an_invalid_cron_schedule_is_reported_clearly(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(YAML.replace('"0 2 * * *"', '"0 25 * * *"'))
    result = run(["schedule", "list", "--config", str(path)])
    assert result.exit_code == 1
    assert "Job 'nightly-backup'" in result.output and "out of range" in result.output


def test_a_prune_job_must_carry_the_safety_switch_for_every_schedule_command(tmp_path, fixed_clock):
    path = tmp_path / "c.yaml"
    path.write_text(YAML.replace("      delete_without_asking: true\n", ""))
    for command in (["list"], ["run", "--max-runs", "1"], ["run-now", "weekly-prune"]):
        result = run(["schedule", *command, "--config", str(path)])
        assert result.exit_code == 1, command
        assert "delete_without_asking: true" in result.output, command


# --------------------------------------------------------------- run-now


@pytest.fixture
def backup_world(monkeypatch):
    adapter, storage = FakeAdapter(content=b"-- dump\n" * 100), FakeStorage()
    monkeypatch.setattr("yourtool.backup.get_adapter", lambda config: adapter)
    monkeypatch.setattr("yourtool.backup.get_storage", lambda config: storage)
    return adapter, storage


def test_run_now_runs_one_job_once(config_file, backup_world):
    _, storage = backup_world
    result = run(["schedule", "run-now", "nightly-backup", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "Running job 'nightly-backup' (backup)" in result.stdout
    assert "finished OK: stored /fake/demo_" in result.stdout
    assert len(storage.files) == 2  # the backup and its checksum


def test_run_now_reports_a_failure_with_exit_code_1(config_file, backup_world):
    adapter, _ = backup_world
    adapter.fail = True
    result = run(["schedule", "run-now", "nightly-backup", "--config", str(config_file)])
    assert result.exit_code == 1 and "failed: database exploded" in result.output


def test_run_now_of_a_prune_job_deletes_as_configured(config_file, monkeypatch):
    files = with_checksums({f"demo_202610{d:02d}_100000.dump": b"x" * 100 for d in range(10, 16)})
    storage = FakeStorage(files=files)
    monkeypatch.setattr(jobs_module, "get_storage", lambda config: storage)
    result = run(["schedule", "run-now", "weekly-prune", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "deleted 4 backup(s)" in result.stdout
    assert sorted(n for n in storage.files if not n.endswith(".sha256")) == [
        "demo_20261014_100000.dump", "demo_20261015_100000.dump"]


def test_run_now_with_an_unknown_job_lists_the_real_ones(config_file):
    result = run(["schedule", "run-now", "nope", "--config", str(config_file)])
    assert result.exit_code == 1 and "no job called 'nope'" in result.output
    assert "nightly-backup" in result.output


def test_run_now_quiet_prints_nothing_on_success(config_file, backup_world):
    result = run(["schedule", "run-now", "nightly-backup", "--config", str(config_file), "-q"])
    assert result.exit_code == 0 and result.output.strip() == ""


# ------------------------------------------------------------------- run


class Harness:
    def __init__(self, start):
        self.now, self.started = start, []

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.now += timedelta(seconds=seconds)
        return False

    def runner(self, outcomes=None):
        outcomes = list(outcomes or [])

        def execute(job):
            self.started.append(job.name)
            self.now += timedelta(seconds=1)
            return outcomes.pop(0) if outcomes else JobOutcome(True, "all good")

        return execute


@pytest.fixture
def fake_time(monkeypatch):
    h = Harness(NOW)
    monkeypatch.setattr(cli, "_clock", h.clock)
    monkeypatch.setattr(cli, "_wait", h.wait)
    monkeypatch.setattr(cli, "execute_job", h.runner())
    return h


def test_run_starts_jobs_in_time_order_and_stops_after_max_runs(config_file, fake_time):
    result = run(["schedule", "run", "--config", str(config_file), "--max-runs", "2"])
    assert result.exit_code == 0, result.output
    # 00:00 Berlin on the 25th (daily-check) comes before 02:00 (nightly-backup)
    assert fake_time.started == ["daily-check", "nightly-backup"]
    out = result.output
    assert "Scheduler started: 3 job(s), time zone Europe/Berlin" in out
    assert "Starting job 'daily-check' (verify)" in out and "Job 'daily-check' finished OK" in out
    assert "Scheduler stopped after 2 job run(s)." in out


def test_run_prints_timestamps_so_you_can_see_when_things_happened(config_file, fake_time):
    out = run(["schedule", "run", "--config", str(config_file), "--max-runs", "1"]).output
    assert "Z [info] Scheduler started" in out
    assert any(line[:4] == "2026" and line[10] == "T" and "Z " in line for line in out.splitlines())


def test_a_failing_job_is_reported_and_the_scheduler_keeps_going(config_file, monkeypatch):
    h = Harness(NOW)
    monkeypatch.setattr(cli, "_clock", h.clock)
    monkeypatch.setattr(cli, "_wait", h.wait)
    monkeypatch.setattr(cli, "execute_job", h.runner([JobOutcome(False, "disk full"), JobOutcome(True, "fine")]))
    result = run(["schedule", "run", "--config", str(config_file), "--max-runs", "2"])
    assert result.exit_code == 0  # the scheduler itself is healthy
    assert "Job 'daily-check' FAILED" in result.output and "disk full" in result.output
    assert "Job 'nightly-backup' finished OK" in result.output


def test_stopping_before_anything_is_due(config_file, monkeypatch):
    monkeypatch.setattr(cli, "_clock", lambda: NOW)
    monkeypatch.setattr(cli, "_wait", lambda seconds: True)  # a stop request arrives immediately
    monkeypatch.setattr(cli, "execute_job", lambda job: pytest.fail("nothing should run"))
    result = run(["schedule", "run", "--config", str(config_file)])
    assert result.exit_code == 0 and "stopped after 0 job run(s)" in result.output


def test_ctrl_c_stops_with_exit_code_130(config_file, monkeypatch):
    h = Harness(NOW)
    monkeypatch.setattr(cli, "_clock", h.clock)
    monkeypatch.setattr(cli, "_wait", h.wait)

    def interrupt(job):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "execute_job", interrupt)
    result = run(["schedule", "run", "--config", str(config_file)])
    assert result.exit_code == 130 and "Interrupted" in result.output


def test_run_warns_when_a_backup_job_has_no_password(config_file, fake_time):
    out = run(["schedule", "run", "--config", str(config_file), "--max-runs", "1"]).output
    assert "no database password is set" in out and "Job 'nightly-backup'" in out


def test_no_warning_when_the_password_is_provided(config_file, fake_time):
    out = run(["schedule", "run", "--config", str(config_file), "--max-runs", "1"],
              env={"YOURTOOL_DB_PASSWORD": "pw"}).output
    assert "no database password" not in out


def test_run_writes_a_log_file(config_file, fake_time, tmp_path):
    logfile = tmp_path / "scheduler.log"
    run(["schedule", "run", "--config", str(config_file), "--max-runs", "1", "--log-file", str(logfile)])
    text = logfile.read_text(encoding="utf-8")
    assert "Scheduler started" in text and "Starting job 'daily-check'" in text
    assert "finished OK" in text and "INFO" in text


def test_run_secrets_are_never_printed(config_file, monkeypatch):
    h = Harness(NOW)
    monkeypatch.setattr(cli, "_clock", h.clock)
    monkeypatch.setattr(cli, "_wait", h.wait)
    monkeypatch.setattr(cli, "execute_job",
                        h.runner([JobOutcome(False, "login failed using hunter2-secret")]))
    result = run(["schedule", "run", "--config", str(config_file), "--max-runs", "1"],
                 env={"YOURTOOL_DB_PASSWORD": "hunter2-secret"})
    assert "hunter2-secret" not in result.output and "********" in result.output


# --------------------------------------------------------------- stopping


@pytest.mark.skipif(not hasattr(signal, "raise_signal"), reason="needs signal.raise_signal")
def test_sigterm_asks_the_scheduler_to_stop():
    """`docker stop` sends SIGTERM; it must set the flag the loop checks."""
    previous = signal.getsignal(signal.SIGTERM)
    try:
        cli._install_stop_signals()
        assert not cli._STOP.is_set()
        signal.raise_signal(signal.SIGTERM)
        assert cli._STOP.is_set()
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_installing_the_signal_handlers_outside_the_main_thread_does_not_crash():
    errors = []

    def attempt():
        try:
            cli._install_stop_signals()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=attempt)
    thread.start()
    thread.join()
    assert errors == []
