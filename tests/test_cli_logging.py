"""The command line wired to logging: --verbose, --quiet, --log-file,
error handling and exit codes. Uses fake adapters, so no database is needed."""

import pytest
from fakes import FakeAdapter, FakeStorage
from typer.testing import CliRunner

import yourtool.cli as cli
from yourtool.databases import DatabaseError

runner = CliRunner()

PASSWORD = "hunter2-pass"
ENV = {"PGPASSWORD": None, "YOURTOOL_DB_PASSWORD": PASSWORD, "YOURTOOL_LOG_FILE": None}
BACKUP = ["backup", "--db-user", "u", "--db-name", "d"]
RESTORE = ["restore", "--db-user", "u", "--target-db", "t", "--file", "d_20261009_100000.dump"]


@pytest.fixture
def fakes(monkeypatch):
    adapter, storage = FakeAdapter(), FakeStorage(files={"d_20261009_100000.dump": b"x"})
    monkeypatch.setattr("yourtool.backup.get_adapter", lambda config: adapter)
    monkeypatch.setattr("yourtool.backup.get_storage", lambda config: storage)
    monkeypatch.setattr("yourtool.restore.get_adapter", lambda config: adapter)
    monkeypatch.setattr("yourtool.restore.get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    return adapter, storage


def run(args, env=ENV, input=None):
    return runner.invoke(cli.app, args, env=env, input=input)


# ------------------------------------------------------- verbose / quiet


def test_default_output(fakes):
    result = run(BACKUP)
    assert result.exit_code == 0
    assert "Backing up 'd'" in result.stdout
    assert "Backup complete" in result.stdout
    assert "[debug]" not in result.output and "[info]" not in result.output


def test_quiet_success_prints_nothing(fakes):
    result = run([*BACKUP, "--quiet"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_quiet_short_flag(fakes):
    assert run([*BACKUP, "-q"]).output.strip() == ""


def test_quiet_still_shows_errors(fakes):
    adapter, _ = fakes
    adapter.fail = True
    result = run([*BACKUP, "-q"])
    assert result.exit_code == 1
    assert "Error: database exploded" in result.output


def test_verbose_shows_detail_on_stderr(fakes):
    result = run([*BACKUP, "--verbose"])
    assert result.exit_code == 0
    assert "[debug] Settings:" in result.stderr
    assert "[info] Starting backup of 'd'" in result.stderr
    assert "[debug]" not in result.stdout  # detail stays off stdout


def test_verbose_and_quiet_together_is_a_usage_error(fakes):
    result = run([*BACKUP, "-v", "-q"])
    assert result.exit_code == 2
    assert "not both" in result.output


def test_verbose_works_on_restore(fakes):
    result = run([*RESTORE, "--yes", "-v"])
    assert result.exit_code == 0, result.output
    assert "[info] Starting restore" in result.stderr


def test_quiet_restore_with_yes_is_silent(fakes):
    result = run([*RESTORE, "--yes", "-q"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""


def test_quiet_restore_still_shows_summary_when_asking_for_confirmation(fakes, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    result = run([*RESTORE, "-q"], input="t\n")
    assert "Target : database 't'" in result.stdout  # you must see what you confirm
    assert "Restore complete" not in result.stdout  # but success stays quiet
    assert result.exit_code == 0


# ------------------------------------------------------------ log file


def test_no_log_file_unless_asked(fakes, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run(BACKUP)
    assert list(tmp_path.iterdir()) == []


def test_log_file_flag_writes_a_run_record(fakes, tmp_path):
    logfile = tmp_path / "logs" / "run.log"
    result = run([*BACKUP, "--log-file", str(logfile)])
    assert result.exit_code == 0
    text = logfile.read_text()
    assert "Starting backup of 'd'" in text
    assert "Backup stored: /fake/d_" in text
    assert "debug" not in text.lower()  # INFO and up unless --verbose


def test_log_file_from_environment(fakes, tmp_path):
    logfile = tmp_path / "env.log"
    run(BACKUP, env={**ENV, "YOURTOOL_LOG_FILE": str(logfile)})
    assert "Starting backup" in logfile.read_text()


def test_log_file_from_config_file(fakes, tmp_path):
    logfile = tmp_path / "cfg.log"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"database:\n  user: u\n  name: d\nlogging:\n  file: {logfile.as_posix()}\n")
    run(["backup", "--config", str(cfg)])
    assert "Starting backup" in logfile.read_text()


def test_quiet_run_still_writes_the_log_file(fakes, tmp_path):
    logfile = tmp_path / "q.log"
    result = run([*BACKUP, "-q", "--log-file", str(logfile)])
    assert result.output.strip() == ""
    assert "Backup stored" in logfile.read_text()  # silent terminal, full record


def test_failures_are_recorded_in_the_log_file(fakes, tmp_path):
    adapter, _ = fakes
    adapter.fail = True
    logfile = tmp_path / "f.log"
    result = run([*BACKUP, "--log-file", str(logfile)])
    assert result.exit_code == 1
    assert "ERROR" in logfile.read_text() and "database exploded" in logfile.read_text()


def test_verbose_log_file_includes_debug(fakes, tmp_path):
    logfile = tmp_path / "v.log"
    run([*BACKUP, "-v", "--log-file", str(logfile)])
    assert "DEBUG" in logfile.read_text()


def test_unusable_log_file_does_not_stop_the_backup(fakes, tmp_path):
    result = run([*BACKUP, "--log-file", str(tmp_path)])  # a folder
    assert result.exit_code == 0
    assert "Could not open log file" in result.output
    assert "Backup complete" in result.stdout


def test_config_show_reports_log_file(fakes, tmp_path):
    result = run(["config", "show", "--db-user", "u", "--db-name", "d", "--log-file", "x.log"])
    assert "log_file" in result.stdout and "x.log" in result.stdout and "[cli]" in result.stdout
    result = run(["config", "show", "--db-user", "u", "--db-name", "d"])
    import re

    assert re.search(r"log_file\s+= \(not set\)", result.stdout)  # column width may change


# ------------------------------------------------------ password safety


def test_password_never_reaches_terminal_or_file_even_if_a_tool_echoes_it(fakes, tmp_path):
    adapter, _ = fakes
    original = adapter.backup

    def leaky_backup(output_path):
        raise DatabaseError(f"connection failed, password was {PASSWORD}")

    adapter.backup = leaky_backup
    logfile = tmp_path / "p.log"
    result = run([*BACKUP, "-v", "--log-file", str(logfile)])

    assert result.exit_code == 1
    assert PASSWORD not in result.output
    assert PASSWORD not in logfile.read_text()
    assert "********" in result.output


def test_typed_password_is_also_protected(fakes, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    adapter, _ = fakes

    def leaky_backup(output_path):
        raise DatabaseError("login failed with typed-secret-9")

    adapter.backup = leaky_backup
    logfile = tmp_path / "t.log"
    env = {**ENV, "YOURTOOL_DB_PASSWORD": None}
    result = run([*BACKUP, "--log-file", str(logfile)], env=env, input="typed-secret-9\n")
    assert "typed-secret-9" not in result.output
    assert "typed-secret-9" not in logfile.read_text()


def test_pgpassword_env_var_is_protected_too(fakes, tmp_path):
    adapter, _ = fakes

    def leaky_backup(output_path):
        raise DatabaseError("auth problem: from-pgpassword-var")

    adapter.backup = leaky_backup
    result = run(BACKUP, env={**ENV, "PGPASSWORD": "from-pgpassword-var"})
    assert "from-pgpassword-var" not in result.output


# ------------------------------------------------ errors and exit codes


def test_unexpected_error_is_clean_and_exits_1(fakes, monkeypatch):
    def bug(config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_backup", bug)
    result = run(BACKUP)
    assert result.exit_code == 1
    assert "Unexpected error (RuntimeError): kaboom" in result.output
    assert "--verbose" in result.output
    assert "Traceback" not in result.output


def test_unexpected_error_shows_traceback_when_verbose(fakes, monkeypatch):
    def bug(config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_backup", bug)
    result = run([*BACKUP, "-v"])
    assert result.exit_code == 1
    assert "Traceback" in result.output


def test_unexpected_error_traceback_goes_to_log_file(fakes, monkeypatch, tmp_path):
    def bug(config):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli, "run_backup", bug)
    logfile = tmp_path / "bug.log"
    result = run([*BACKUP, "--log-file", str(logfile)])
    assert "Traceback" not in result.output  # terminal stays tidy
    assert "Traceback" in logfile.read_text()  # but the record is complete


def test_ctrl_c_exits_130_with_a_message(fakes, monkeypatch):
    def interrupted(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_backup", interrupted)
    result = run(BACKUP)
    assert result.exit_code == 130
    assert "Interrupted" in result.output


def test_ctrl_c_during_restore_exits_130(fakes, monkeypatch):
    def interrupted(config, name, *, clean=False, confirm):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_restore", interrupted)
    result = run([*RESTORE, "--yes"])
    assert result.exit_code == 130
    assert "single transaction" in result.output


def test_config_errors_exit_1_with_message(fakes):
    result = run(["backup"], env={**ENV})  # no user / db name
    assert result.exit_code == 1
    assert "Missing required" in result.output


def test_exit_codes_summary(fakes):
    assert run(BACKUP).exit_code == 0
    assert run(["backup"]).exit_code == 1
    assert run([*BACKUP, "--no-such-flag"]).exit_code == 2
