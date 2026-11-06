import pytest
from fakes import FakeStorage
from typer.testing import CliRunner

import yourtool.cli as cli
from yourtool.restore import RestoreCancelled, RestorePlan, RestoreResult

runner = CliRunner()

OLD = "d_20261009_100000.dump"
NEW = "d_20261010_100000.dump"
# None removes a variable, so the host machine's own settings can't leak in.
ENV = {"PGPASSWORD": None, "YOURTOOL_DB_PASSWORD": "pw", "YOURTOOL_DB_NAME": None}
BASE = ["restore", "--db-user", "u", "--target-db", "target"]


@pytest.fixture
def world(monkeypatch):
    state = {"exists": True, "ran": False, "interactive": True}
    storage = FakeStorage(files={OLD: b"x", NEW: b"yy", "notes.txt": b"z"})

    def fake_run_restore(config, name, *, clean=False, confirm):
        state.update(config=config, name=name, clean=clean)
        plan = RestorePlan(
            backup_name=name, target_db=config.db_name, host=config.db_host,
            port=config.db_port, target_exists=state["exists"], clean=clean,
        )
        if not confirm(plan):
            raise RestoreCancelled("Restore cancelled. Nothing was changed.")
        state["ran"] = True
        return RestoreResult(
            backup_name=name, target_db=config.db_name, created_database=not state["exists"]
        )

    monkeypatch.setattr(cli, "get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "run_restore", fake_run_restore)
    monkeypatch.setattr(cli, "_is_interactive", lambda: state["interactive"])
    return state


def run(args, world, input=None, env=ENV):
    return runner.invoke(cli.app, args, env=env, input=input)


# ---- choosing the backup


def test_default_choice_is_newest_and_non_backup_files_hidden(world):
    result = run(BASE + ["--yes"], world, input="\n")
    assert result.exit_code == 0, result.output
    assert world["name"] == NEW
    assert "notes.txt" not in result.stdout
    assert result.stdout.index(NEW) < result.stdout.index(OLD)  # newest first


def test_pick_second_backup(world):
    run(BASE + ["--yes"], world, input="2\n")
    assert world["name"] == OLD


def test_out_of_range_choice_is_reprompted(world):
    result = run(BASE + ["--yes"], world, input="9\n2\n")
    assert result.exit_code == 0
    assert world["name"] == OLD


def test_file_flag_skips_the_list(world):
    world["interactive"] = False
    result = run(BASE + ["--file", OLD, "--yes"], world)
    assert result.exit_code == 0
    assert world["name"] == OLD
    assert "Available backups" not in result.stdout


def test_non_interactive_without_file_fails(world):
    world["interactive"] = False
    result = run(BASE + ["--yes"], world)
    assert result.exit_code == 1
    assert "--file" in result.output
    assert world["ran"] is False


def test_no_backups_found(world, monkeypatch):
    monkeypatch.setattr(cli, "get_storage", lambda config: FakeStorage())
    result = run(BASE + ["--yes"], world)
    assert result.exit_code == 1
    assert "No backups found" in result.output


# ---- confirmation


def test_existing_target_wrong_confirmation_cancels(world):
    result = run(BASE + ["--file", NEW], world, input="nope\n")
    assert result.exit_code == 1
    assert "Nothing was changed" in result.output
    assert world["ran"] is False


def test_existing_target_empty_confirmation_cancels(world):
    result = run(BASE + ["--file", NEW], world, input="\n")
    assert result.exit_code == 1
    assert world["ran"] is False


def test_existing_target_right_confirmation_proceeds(world):
    result = run(BASE + ["--file", NEW], world, input="target\n")
    assert result.exit_code == 0, result.output
    assert world["ran"] is True
    assert "Restore complete" in result.stdout


def test_yes_skips_confirmation(world):
    result = run(BASE + ["--file", NEW, "--yes"], world)
    assert result.exit_code == 0
    assert world["ran"] is True
    assert "to confirm" not in result.stdout


def test_non_interactive_existing_target_needs_yes(world):
    world["interactive"] = False
    result = run(BASE + ["--file", NEW], world)
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert world["ran"] is False


def test_new_target_needs_no_confirmation(world):
    world["exists"] = False
    world["interactive"] = False
    result = run(BASE + ["--file", NEW], world)
    assert result.exit_code == 0, result.output
    assert "will be created" in result.stdout
    assert "created and restored" in result.stdout


def test_summary_describes_clean_mode(world):
    result = run(BASE + ["--file", NEW, "--clean", "--yes"], world)
    assert world["clean"] is True
    assert "dropped and recreated" in result.stdout


# ---- target database safety


def test_target_db_is_required(world):
    result = run(["restore", "--db-user", "u", "--file", NEW, "--yes"], world)
    assert result.exit_code == 2
    assert world["ran"] is False


def test_target_db_overrides_database_name_from_environment(world):
    env = {**ENV, "YOURTOOL_DB_NAME": "production_db"}
    run(BASE + ["--file", NEW, "--yes"], world, env=env)
    assert world["config"].db_name == "target"


def test_restore_has_no_db_name_flag(world):
    result = run(BASE + ["--db-name", "x", "--file", NEW, "--yes"], world)
    assert result.exit_code != 0


# ---- password


def test_password_prompt_when_not_set(world):
    env = {**ENV, "YOURTOOL_DB_PASSWORD": None}
    result = run(BASE + ["--file", NEW, "--yes"], world, input="typed\n", env=env)
    assert result.exit_code == 0, result.output
    assert world["config"].db_password == "typed"
    assert "typed" not in result.stdout


def test_error_from_workflow_exits_1(world, monkeypatch):
    from yourtool.databases import DatabaseError

    def boom(config, name, *, clean=False, confirm):
        raise DatabaseError("server unreachable")

    monkeypatch.setattr(cli, "run_restore", boom)
    result = run(BASE + ["--file", NEW, "--yes"], world)
    assert result.exit_code == 1
    assert "server unreachable" in result.output
