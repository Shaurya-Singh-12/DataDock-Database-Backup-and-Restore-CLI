import pytest
from typer.testing import CliRunner

import yourtool.cli as cli
from yourtool.backup import BackupResult
from yourtool.databases import DatabaseError

runner = CliRunner()

BASE_ARGS = ["backup", "--db-user", "u", "--db-name", "d"]
# None removes a variable, so the host machine's own settings can't leak in.
CLEAN_ENV = {"PGPASSWORD": None, "YOURTOOL_DB_PASSWORD": None}


@pytest.fixture
def captured(monkeypatch):
    """Replace the real workflow; remember the config it was called with."""
    seen = {}

    def fake_run_backup(config):
        seen["config"] = config
        return BackupResult(name="d_x.dump", location="/b/d_x.dump", size=2048)

    monkeypatch.setattr(cli, "run_backup", fake_run_backup)
    return seen


def interactive(monkeypatch, value=True):
    monkeypatch.setattr(cli, "_is_interactive", lambda: value)


def test_success_output(captured, monkeypatch):
    interactive(monkeypatch, False)
    result = runner.invoke(cli.app, BASE_ARGS, env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "Backup complete: /b/d_x.dump (2.0 KB)" in result.stdout


def test_prompts_for_password_when_interactive(captured, monkeypatch):
    interactive(monkeypatch)
    result = runner.invoke(cli.app, BASE_ARGS, env=CLEAN_ENV, input="typed-secret\n")
    assert result.exit_code == 0
    assert captured["config"].db_password == "typed-secret"
    assert "typed-secret" not in result.stdout  # hidden input is not echoed


def test_empty_prompt_answer_means_no_password(captured, monkeypatch):
    interactive(monkeypatch)
    runner.invoke(cli.app, BASE_ARGS, env=CLEAN_ENV, input="\n")
    assert captured["config"].db_password is None


def test_no_prompt_when_env_password_set(captured, monkeypatch):
    interactive(monkeypatch)
    env = {**CLEAN_ENV, "YOURTOOL_DB_PASSWORD": "from-env"}
    result = runner.invoke(cli.app, BASE_ARGS, env=env)
    assert result.exit_code == 0
    assert "Password for" not in result.stdout
    assert captured["config"].db_password == "from-env"


def test_no_prompt_when_pgpassword_set(captured, monkeypatch):
    interactive(monkeypatch)
    env = {**CLEAN_ENV, "PGPASSWORD": "standard"}
    result = runner.invoke(cli.app, BASE_ARGS, env=env)
    assert "Password for" not in result.stdout


def test_no_prompt_flag(captured, monkeypatch):
    interactive(monkeypatch)
    result = runner.invoke(cli.app, [*BASE_ARGS, "--no-prompt"], env=CLEAN_ENV)
    assert "Password for" not in result.stdout
    assert captured["config"].db_password is None


def test_no_prompt_when_not_interactive(captured, monkeypatch):
    interactive(monkeypatch, False)
    result = runner.invoke(cli.app, BASE_ARGS, env=CLEAN_ENV)
    assert "Password for" not in result.stdout


def test_workflow_error_exits_1(monkeypatch):
    interactive(monkeypatch, False)

    def boom(config):
        raise DatabaseError("could not connect")

    monkeypatch.setattr(cli, "run_backup", boom)
    result = runner.invoke(cli.app, BASE_ARGS, env=CLEAN_ENV)
    assert result.exit_code == 1
    assert "could not connect" in result.output


def test_config_error_exits_1(monkeypatch):
    interactive(monkeypatch, False)
    result = runner.invoke(cli.app, ["backup"], env=CLEAN_ENV)  # no user/db name
    assert result.exit_code == 1
    assert "Missing required" in result.output


def test_password_flag_does_not_exist():
    result = runner.invoke(cli.app, [*BASE_ARGS, "--db-password", "x"], env=CLEAN_ENV)
    assert result.exit_code != 0
