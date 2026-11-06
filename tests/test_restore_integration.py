"""End to end: backup then restore against a REAL PostgreSQL server."""

import pytest
from typer.testing import CliRunner

from yourtool.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def stored_backup(source_db, tmp_path):
    result = runner.invoke(
        app, ["backup", "--db-name", source_db, "--path", str(tmp_path), "--no-prompt"]
    )
    assert result.exit_code == 0, result.output
    (backup,) = tmp_path.glob(f"{source_db}_*.dump")
    return tmp_path, backup.name


def restore(path, name, target, *extra):
    return runner.invoke(
        app,
        ["restore", "--target-db", target, "--path", str(path), "--file", name,
         "--no-prompt", *extra],
    )


def test_full_round_trip(pg, make_database, stored_backup):
    path, name = stored_backup
    target = make_database("yt_restore", create=False)

    # 1. New database: created and filled, no confirmation needed.
    result = restore(path, name, target)
    assert result.exit_code == 0, result.output
    assert "created and restored" in result.stdout
    assert pg.count(target) == 3

    # 2. Existing database, not interactive, no --yes: refused, untouched.
    pg.psql("DELETE FROM students WHERE name = 'Bob';", target)
    result = restore(path, name, target)
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert pg.count(target) == 2

    # 3. With --yes but no --clean: names clash, so it fails AND rolls back.
    result = restore(path, name, target, "--yes")
    assert result.exit_code == 1
    assert "--clean" in result.output
    assert pg.count(target) == 2  # unchanged: all-or-nothing

    # 4. With --clean --yes: objects replaced, data back.
    result = restore(path, name, target, "--yes", "--clean")
    assert result.exit_code == 0, result.output
    assert pg.count(target) == 3


def test_clean_leaves_unrelated_tables_alone(pg, make_database, stored_backup):
    path, name = stored_backup
    target = make_database("yt_restore", create=False)
    assert restore(path, name, target).exit_code == 0
    pg.psql("CREATE TABLE extra (id int);", target)

    assert restore(path, name, target, "--yes", "--clean").exit_code == 0
    assert pg.psql(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='extra';", target
    ) == "1"


def test_restore_into_a_different_existing_database(pg, make_database, stored_backup):
    path, name = stored_backup
    target = make_database("yt_other")  # exists, empty
    result = restore(path, name, target, "--yes")
    assert result.exit_code == 0, result.output
    assert "created and restored" not in result.stdout  # it already existed
    assert pg.count(target) == 3


def test_corrupt_backup_is_rejected_and_no_database_created(pg, make_database, tmp_path):
    (tmp_path / "x_20261009_100000.dump").write_bytes(b"this is not a backup")
    target = make_database("yt_restore", create=False)
    result = restore(tmp_path, "x_20261009_100000.dump", target)
    assert result.exit_code == 1
    assert "not a valid backup" in result.output
    assert pg.database_exists(target) is False
