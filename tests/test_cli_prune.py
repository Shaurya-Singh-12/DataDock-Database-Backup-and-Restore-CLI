"""`yourtool prune`: preview, confirmation, scope and failure handling."""

import re

import pytest
from fakes import FakeStorage, with_checksums
from typer.testing import CliRunner

import yourtool.cli as cli

runner = CliRunner()


def backup_name(day, db="d", ext=".dump"):
    return f"{db}_202610{day:02d}_100000{ext}"


def make_files():
    """6 backups of 'd' (10th to 15th), 3 of 'other', and a file that is not ours."""
    files = {backup_name(day): b"x" * 1000 for day in range(10, 16)}
    files.update({backup_name(day, "other"): b"y" * 500 for day in (1, 2, 3)})
    files = with_checksums(files)
    files["notes.txt"] = b"my own notes"
    return files


@pytest.fixture
def world(monkeypatch):
    storage = FakeStorage(files=make_files())
    state = {"interactive": False}
    monkeypatch.setattr(cli, "get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "_is_interactive", lambda: state["interactive"])
    return storage, state


def run(args, input=None, env=None):
    return runner.invoke(cli.app, args, env=env, input=input)


PRUNE = ["prune", "--db-name", "d", "--keep-last", "2"]


# ------------------------------------------------------ refusing to guess


def test_no_rule_given_deletes_nothing(world):
    storage, _ = world
    result = run(["prune", "--db-name", "d", "--yes"])
    assert result.exit_code == 1
    assert "No retention rule was given" in result.output
    assert storage.deleted == []


def test_no_database_named_deletes_nothing(world):
    storage, _ = world
    result = run(["prune", "--keep-last", "2", "--yes"])
    assert result.exit_code == 1 and "Say which database" in result.output
    assert storage.deleted == []


def test_db_name_and_all_databases_together_is_a_usage_error(world):
    assert run([*PRUNE, "--all-databases"]).exit_code == 2


def test_dry_run_with_yes_is_a_usage_error(world):
    assert run([*PRUNE, "--dry-run", "--yes"]).exit_code == 2


@pytest.mark.parametrize("bad", ["0", "-3", "abc"])
def test_bad_rule_values_are_rejected(world, bad):
    storage, _ = world
    result = run(["prune", "--db-name", "d", "--yes", "--keep-daily", bad])
    assert result.exit_code != 0
    assert storage.deleted == []


def test_zero_from_the_environment_is_rejected_too(world):
    result = run(["prune", "--db-name", "d", "--yes"], env={"YOURTOOL_KEEP_LAST": "0"})
    assert result.exit_code == 1 and "whole number of 1 or more" in result.output


# --------------------------------------------------------------- dry run


def test_dry_run_shows_the_plan_and_deletes_nothing(world):
    storage, _ = world
    before = dict(storage.files)
    result = run([*PRUNE, "--dry-run"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Rules: keep last 2" in out
    assert re.search(r"KEEP\s+d_20261015_100000\.dump\s+1000 B\s+\(last\)", out)
    assert re.search(r"KEEP\s+d_20261014_100000\.dump", out)
    for day in (13, 12, 11, 10):
        assert re.search(rf"DELETE\s+d_202610{day}_100000\.dump\s+1000 B \+ its checksum file", out)
    assert "Keeping 2 of 6 backup(s). Deleting 4" in out
    assert "Dry run: nothing was deleted." in out
    assert storage.files == before and storage.deleted == []


def test_dry_run_only_ever_mentions_the_chosen_database(world):
    out = run([*PRUNE, "--dry-run"]).stdout
    assert "other_" not in out and "notes.txt" not in out


def test_quiet_does_not_hide_a_dry_run(world):
    assert "DELETE" in run([*PRUNE, "--dry-run", "-q"]).stdout


# ------------------------------------------------------------ confirmation


def test_without_a_terminal_and_without_yes_nothing_is_deleted(world):
    storage, _ = world
    result = run(PRUNE)
    assert result.exit_code == 1
    assert "Refusing to delete without confirmation" in result.output
    assert storage.deleted == []


def test_typing_delete_confirms(world):
    storage, state = world
    state["interactive"] = True
    result = run(PRUNE, input="delete\n")
    assert result.exit_code == 0, result.output
    assert "Type 'delete' to permanently delete these 4 backup(s)" in result.stdout
    assert "Deleted 4 backup(s)" in result.stdout and "Kept 2" in result.stdout
    assert len(storage.deleted) == 8  # 4 backups + 4 checksum files


@pytest.mark.parametrize("answer", ["yes", "y", "DELETE", "Delete", "", "delete please", " delete"])
def test_anything_other_than_exactly_delete_cancels(world, answer):
    storage, state = world
    state["interactive"] = True
    result = run(PRUNE, input=f"{answer}\n")
    assert result.exit_code == 1 and "Cancelled. Nothing was deleted." in result.output
    assert storage.deleted == []


def test_the_plan_is_shown_before_the_question(world):
    _, state = world
    state["interactive"] = True
    out = run(PRUNE, input="no\n").stdout
    assert out.index("DELETE") < out.index("Type 'delete'")


def test_quiet_still_shows_the_plan_when_asking_a_person(world):
    _, state = world
    state["interactive"] = True
    out = run([*PRUNE, "-q"], input="no\n").stdout
    assert "DELETE" in out


def test_yes_deletes_without_asking(world):
    storage, _ = world
    result = run([*PRUNE, "--yes"])
    assert result.exit_code == 0, result.output
    assert "Type 'delete'" not in result.stdout
    assert sorted(n for n in storage.files if n.startswith("d_") and not n.endswith(".sha256")) == [
        backup_name(14), backup_name(15)]


def test_quiet_with_yes_is_silent(world):
    storage, _ = world
    result = run([*PRUNE, "--yes", "-q"])
    assert result.exit_code == 0 and result.output.strip() == ""
    assert len(storage.deleted) == 8


# ---------------------------------------------------------------- what is deleted


def test_checksum_files_go_with_their_backup_and_the_backup_goes_first(world):
    storage, _ = world
    run([*PRUNE, "--yes"])
    for day in (10, 11, 12, 13):
        name = backup_name(day)
        assert name not in storage.files and name + ".sha256" not in storage.files
        assert storage.deleted.index(name) < storage.deleted.index(name + ".sha256")
    for day in (14, 15):  # the kept ones keep their checksum files
        assert backup_name(day) + ".sha256" in storage.files


def test_other_databases_and_foreign_files_are_never_touched(world):
    storage, _ = world
    run([*PRUNE, "--yes"])
    for day in (1, 2, 3):
        assert backup_name(day, "other") in storage.files
        assert backup_name(day, "other") + ".sha256" in storage.files
    assert storage.files["notes.txt"] == b"my own notes"


def test_all_databases_applies_the_rules_to_each_separately(world):
    storage, _ = world
    result = run(["prune", "--all-databases", "--keep-last", "1", "--yes"])
    assert result.exit_code == 0, result.output
    remaining = sorted(n for n in storage.files if not n.endswith(".sha256") and n != "notes.txt")
    assert remaining == sorted([backup_name(15), backup_name(3, "other")])  # the newest of each
    assert "notes.txt" in storage.files


def test_when_every_backup_is_kept_there_is_nothing_to_do_and_no_question(world):
    storage, state = world
    state["interactive"] = True
    result = run(["prune", "--db-name", "d", "--keep-last", "50"])
    assert result.exit_code == 0
    assert "Nothing to delete: all 6 backup(s) are kept" in result.stdout
    assert "Type 'delete'" not in result.stdout and storage.deleted == []


def test_no_backups_of_that_database(world):
    result = run(["prune", "--db-name", "nosuch", "--keep-last", "1", "--yes"])
    assert result.exit_code == 0 and "No backups of database 'nosuch' found" in result.stdout


def test_orphan_checksum_files_are_reported_and_left_alone(world):
    storage, _ = world
    storage.files[backup_name(1, "d") + ".sha256"] = b"0" * 64 + b"  gone.dump\n"
    out = run([*PRUNE, "--dry-run"]).stdout
    assert "1 checksum file(s) have no matching backup and are left alone" in out
    run([*PRUNE, "--yes"])
    assert backup_name(1, "d") + ".sha256" in storage.files


def test_reasons_are_shown_for_every_kept_backup(world):
    out = run(["prune", "--db-name", "d", "--keep-last", "1", "--keep-daily", "2", "--dry-run"]).stdout
    assert re.search(r"KEEP\s+d_20261015_100000\.dump\s+1000 B\s+\(last, daily\)", out)
    assert re.search(r"KEEP\s+d_20261014_100000\.dump\s+1000 B\s+\(daily\)", out)


# ------------------------------------------------------------- failures


def test_a_failure_is_reported_the_rest_still_runs_and_the_exit_code_is_1(world):
    storage, _ = world
    storage.fail_delete.add(backup_name(12))
    result = run([*PRUNE, "--yes"])
    assert result.exit_code == 1
    assert f"FAILED to delete {backup_name(12)}: permission denied" in result.stdout
    assert "Deleted 3 backup(s)" in result.stdout
    assert backup_name(12) in storage.files and backup_name(12) + ".sha256" in storage.files
    assert backup_name(11) not in storage.files


# ----------------------------------------------------------- where rules come from


def test_rules_from_the_config_file(world, tmp_path):
    storage, _ = world
    cfg = tmp_path / "c.yaml"
    cfg.write_text("database:\n  name: d\nretention:\n  keep_last: 3\n")
    result = run(["prune", "--config", str(cfg), "--dry-run"])
    assert "Rules: keep last 3" in result.stdout and "Deleting 3" in result.stdout


def test_rules_from_the_environment_and_flags_override_them(world):
    out = run(["prune", "--db-name", "d", "--dry-run"], env={"YOURTOOL_KEEP_LAST": "4"}).stdout
    assert "Rules: keep last 4" in out
    out = run(["prune", "--db-name", "d", "--dry-run", "--keep-last", "5"],
              env={"YOURTOOL_KEEP_LAST": "4"}).stdout
    assert "Rules: keep last 5" in out


def test_the_database_name_can_come_from_the_config_file(world, tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("database:\n  name: d\n  user: u\nretention:\n  keep_daily: 1\n")
    out = run(["prune", "--config", str(cfg), "--dry-run"]).stdout
    assert "Database 'd'" in out and "daily" in out


def test_config_show_lists_the_rules():
    out = run(["config", "show", "--db-user", "u", "--db-name", "d", "--keep-last", "3"]).stdout
    assert re.search(r"keep_last\s+= 3\s+\[cli\]", out)
    assert re.search(r"keep_daily\s+= \(not set\)", out)


# --------------------------------------------------------------- the audit trail


def test_deletions_are_written_to_the_log_file(world, tmp_path):
    logfile = tmp_path / "prune.log"
    run([*PRUNE, "--yes", "-q", "--log-file", str(logfile)])
    text = logfile.read_text(encoding="utf-8")
    assert "Retention rules: keep last 2" in text
    for day in (10, 11, 12, 13):
        assert f"Deleted backup {backup_name(day)}" in text
        assert f"Deleted checksum file {backup_name(day)}.sha256" in text
    assert f"Deleted backup {backup_name(15)}" not in text


def test_storage_errors_exit_1_cleanly(monkeypatch):
    from yourtool.storage import StorageError

    class Broken(FakeStorage):
        def list(self):
            raise StorageError("Access denied to s3://b: the credentials need s3:ListBucket")

    monkeypatch.setattr(cli, "get_storage", lambda config: Broken())
    result = run([*PRUNE, "--yes"])
    assert result.exit_code == 1 and "Access denied" in result.output
    assert "Traceback" not in result.output
