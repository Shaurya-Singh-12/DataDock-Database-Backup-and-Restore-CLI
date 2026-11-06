"""Password handling on the command line when the database is MySQL."""

import pytest
from typer.testing import CliRunner

import yourtool.cli as cli
from yourtool.backup import BackupResult

runner = CliRunner()
ARGS = ["backup", "--db-type", "mysql", "--db-user", "u", "--db-name", "d"]


@pytest.fixture
def seen(monkeypatch):
    seen = {}

    def fake_run_backup(config):
        seen["config"] = config
        return BackupResult(name="d_x.sql", location="/b/d_x.sql", size=10)

    monkeypatch.setattr(cli, "run_backup", fake_run_backup)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    return seen


def test_prompts_when_nothing_is_set(seen):
    result = runner.invoke(cli.app, ARGS, input="typed\n")
    assert "Password for u@localhost" in result.stdout
    assert seen["config"].db_password == "typed"
    assert seen["config"].db_port == 3306


def test_no_prompt_when_mysql_pwd_is_set(seen):
    result = runner.invoke(cli.app, ARGS, env={"MYSQL_PWD": "already"})
    assert "Password for" not in result.stdout


def test_postgres_variable_does_not_count_for_mysql(seen):
    # PGPASSWORD is for PostgreSQL; with MySQL we must still ask.
    result = runner.invoke(cli.app, ARGS, env={"PGPASSWORD": "pg"}, input="typed\n")
    assert "Password for u@localhost" in result.stdout
    assert seen["config"].db_password == "typed"


def test_mysql_variable_does_not_count_for_postgres(seen):
    args = ["backup", "--db-user", "u", "--db-name", "d"]
    result = runner.invoke(cli.app, args, env={"MYSQL_PWD": "my"}, input="typed\n")
    assert "Password for u@localhost" in result.stdout


def test_mysql_pwd_value_is_never_printed(seen, monkeypatch):
    def leaky(config):
        from yourtool.databases import DatabaseError
        raise DatabaseError("login failed using hunter2-mysql")

    monkeypatch.setattr(cli, "run_backup", leaky)
    result = runner.invoke(cli.app, ARGS, env={"MYSQL_PWD": "hunter2-mysql"})
    assert result.exit_code == 1
    assert "hunter2-mysql" not in result.output
