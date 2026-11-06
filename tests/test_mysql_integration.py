"""MySQL/MariaDB against a REAL server. Opt in with YOURTOOL_INTEGRATION_MYSQL=1.

PowerShell:
    $env:YOURTOOL_INTEGRATION_MYSQL = '1'
    $env:YOURTOOL_MYSQL_USER = 'root'
    $env:YOURTOOL_MYSQL_PASSWORD = 'your-password'
    pytest tests/test_mysql_integration.py

Every test uses its own throwaway database; your own databases are never touched.
"""

import uuid

import pytest
from my_helpers import quote_identifier, sql_string
from typer.testing import CliRunner

from yourtool.cli import app
from yourtool.config import resolve_config
from yourtool.databases import DatabaseError, MySqlAdapter, get_adapter

pytestmark = pytest.mark.mysql_integration

runner = CliRunner()


def adapter_for(db_name: str, **cli) -> MySqlAdapter:
    config, _ = resolve_config(cli={"db_name": db_name, **cli})
    return get_adapter(config)


# ----------------------------------------------------------------- adapter


def test_real_backup_is_a_finished_dump(my, my_source_db, tmp_path):
    out = tmp_path / "real.sql"
    adapter = adapter_for(my_source_db)
    adapter.backup(out)

    text = out.read_text(encoding="utf-8")
    assert "CREATE TABLE `students`" in text
    assert "Zo\u00eb" in text  # non-ASCII data survives
    assert "count_students" in text  # routines are included
    assert "top_students" in text  # views are included
    adapter.verify_backup(out)  # accepted by our own check


def test_backup_of_missing_database_fails_cleanly(my, tmp_path):
    out = tmp_path / "nope.sql"
    with pytest.raises(DatabaseError, match="does not exist|not allowed"):
        adapter_for("yt_does_not_exist").backup(out)
    assert not out.exists()


def test_wrong_password_gives_a_helpful_message(my, my_source_db, tmp_path):
    with pytest.raises(DatabaseError, match="YOURTOOL_DB_PASSWORD"):
        adapter_for(my_source_db, db_password="definitely-wrong-password").backup(tmp_path / "x.sql")


def test_database_exists_and_create(my, my_source_db, make_my_database):
    assert adapter_for(my_source_db).database_exists() is True
    new_name = make_my_database("yt_new", create=False)
    adapter = adapter_for(new_name)
    assert adapter.database_exists() is False
    adapter.create_database()
    assert adapter.database_exists() is True
    assert my.database_exists(new_name)


@pytest.mark.parametrize("fancy", ["yt_b`tick_{}", "yt space_{}", "yt'apos_{}"])
def test_unusual_database_names_are_quoted_safely(my, make_my_database, fancy):
    name = make_my_database(name=fancy.format(uuid.uuid4().hex[:6]), create=False)
    adapter = adapter_for(name)
    assert adapter.database_exists() is False
    adapter.create_database()
    assert adapter.database_exists() is True
    assert my.database_exists(name)


def test_special_characters_in_a_password_work_for_a_restricted_user(my, make_my_database, tmp_path):
    """A password with quotes, backslashes and # goes through the option file intact,
    and a user limited to one database can still back it up."""
    db = make_my_database("yt_priv")
    my.sql("CREATE TABLE t (id INT); INSERT INTO t VALUES (1), (2);", db)
    user, password = f"yt_u_{uuid.uuid4().hex[:6]}", 'p"a\\ss#wo\'rd x'
    my.sql(f"CREATE USER {sql_string(user)}@'%' IDENTIFIED BY {sql_string(password)}")
    try:
        my.sql(f"GRANT ALL ON {quote_identifier(db)}.* TO {sql_string(user)}@'%'")
        out = tmp_path / "priv.sql"
        adapter_for(db, db_user=user, db_password=password).backup(out)
        assert "INSERT INTO `t`" in out.read_text(encoding="utf-8")
    finally:
        my.sql(f"DROP USER IF EXISTS {sql_string(user)}@'%'")


def test_verify_rejects_truncated_and_foreign_files(my, tmp_path):
    adapter = adapter_for("anything")
    cut = tmp_path / "cut.sql"
    cut.write_text("-- MySQL dump 10.13\nCREATE TABLE t (id int);\n")
    with pytest.raises(DatabaseError, match="incomplete"):
        adapter.verify_backup(cut)
    pg = tmp_path / "pg.sql"
    pg.write_bytes(b"PGDMP\x01\x0e\x00")
    with pytest.raises(DatabaseError, match="not a valid MySQL/MariaDB backup"):
        adapter.verify_backup(pg)


# --------------------------------------------------------------- the CLI


@pytest.fixture
def stored_backup(my, my_source_db, tmp_path):
    result = runner.invoke(
        app, ["backup", "--db-name", my_source_db, "--path", str(tmp_path), "--no-prompt"]
    )
    assert result.exit_code == 0, result.output
    (backup,) = tmp_path.glob(f"{my_source_db}_*.sql")
    return tmp_path, backup.name


def restore(path, name, target, *extra):
    return runner.invoke(
        app,
        ["restore", "--target-db", target, "--path", str(path), "--file", name,
         "--no-prompt", *extra],
    )


def test_cli_backup_uses_sql_extension_and_default_port(my, my_source_db, tmp_path):
    result = runner.invoke(
        app, ["backup", "--db-name", my_source_db, "--path", str(tmp_path), "--no-prompt"]
    )
    assert result.exit_code == 0, result.output
    assert list(tmp_path.glob("*.sql")) and not list(tmp_path.glob("*.dump"))


def test_full_round_trip(my, make_my_database, stored_backup):
    path, name = stored_backup
    target = make_my_database("yt_restore", create=False)

    # 1. New database: created and filled, no confirmation needed. Everything survives.
    result = restore(path, name, target)
    assert result.exit_code == 0, result.output
    assert "created and restored" in result.stdout
    assert my.count(target) == 4
    assert my.sql("SELECT name FROM students WHERE id = 4", target) == "Zo\u00eb"
    assert my.sql("SELECT COUNT(*) FROM top_students", target) == "3"  # view works
    assert my.sql("CALL count_students()", target) == "4"  # procedure works

    # 2. Existing database, not interactive, no --yes: refused, untouched.
    my.sql("DELETE FROM students WHERE name = 'Bob'", target)
    result = restore(path, name, target)
    assert result.exit_code == 1 and "--yes" in result.output
    assert my.count(target) == 3

    # 3. With --yes but no --clean: not empty, so refused BEFORE changing anything.
    result = restore(path, name, target, "--yes")
    assert result.exit_code == 1
    assert "not empty" in result.output and "--clean" in result.output
    assert my.count(target) == 3  # unchanged

    # 4. With --clean --yes: objects replaced, data back.
    result = restore(path, name, target, "--yes", "--clean")
    assert result.exit_code == 0, result.output
    assert my.count(target) == 4


def test_clean_leaves_unrelated_tables_alone(my, make_my_database, stored_backup):
    path, name = stored_backup
    target = make_my_database("yt_restore", create=False)
    assert restore(path, name, target).exit_code == 0
    my.sql("CREATE TABLE extra (id INT)", target)

    assert restore(path, name, target, "--yes", "--clean").exit_code == 0
    assert my.sql("SELECT COUNT(*) FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'extra'", target) == "1"


def test_restore_into_an_existing_empty_database(my, make_my_database, stored_backup):
    path, name = stored_backup
    target = make_my_database("yt_empty")  # exists, empty
    result = restore(path, name, target, "--yes")
    assert result.exit_code == 0, result.output
    assert "created and restored" not in result.stdout
    assert my.count(target) == 4


def test_corrupt_backup_is_rejected_and_no_database_created(my, make_my_database, tmp_path):
    (tmp_path / "x_20261009_100000.sql").write_text("-- MySQL dump 10.13\nCREATE TABLE t (id int);\n")
    target = make_my_database("yt_restore", create=False)
    result = restore(tmp_path, "x_20261009_100000.sql", target)
    assert result.exit_code == 1
    assert "incomplete" in result.output
    assert my.database_exists(target) is False


def test_failed_restore_into_a_new_database_removes_it(my, make_my_database, tmp_path):
    """The file looks like a finished dump but contains broken SQL."""
    bad = tmp_path / "x_20261009_100000.sql"
    bad.write_text(
        "-- MySQL dump 10.13\nCREATE TABLE ok (id int);\nTHIS IS NOT SQL;\n"
        "-- Dump completed on 2026-10-10 12:00:00\n"
    )
    target = make_my_database("yt_restore", create=False)
    result = restore(tmp_path, bad.name, target)
    assert result.exit_code == 1
    assert "removed again" in result.output
    assert my.database_exists(target) is False  # nothing left behind


def test_failed_restore_with_clean_warns_it_may_be_partial(my, make_my_database, tmp_path):
    bad = tmp_path / "x_20261009_100000.sql"
    bad.write_text(
        "-- MySQL dump 10.13\nCREATE TABLE ok (id int);\nTHIS IS NOT SQL;\n"
        "-- Dump completed on 2026-10-10 12:00:00\n"
    )
    target = make_my_database("yt_existing")
    my.sql("CREATE TABLE keepme (id INT)", target)
    result = restore(tmp_path, bad.name, target, "--yes", "--clean")
    assert result.exit_code == 1
    assert "partly restored" in result.output
    assert my.database_exists(target)  # a database we did not create is never dropped


def test_password_never_appears_in_the_log_file(my, my_source_db, tmp_path):
    logfile = tmp_path / "run.log"
    result = runner.invoke(
        app,
        ["backup", "--db-name", my_source_db, "--path", str(tmp_path / "b"), "--no-prompt",
         "--verbose", "--log-file", str(logfile)],
    )
    assert result.exit_code == 0, result.output
    text = logfile.read_text(encoding="utf-8")
    assert "Backup stored" in text and "mysqldump" in text.lower() or "mariadb-dump" in text.lower()
    if len(my.password) >= 4:
        assert my.password not in text
        assert my.password not in result.output


def test_the_port_setting_is_honoured_even_for_localhost(my, my_source_db, tmp_path):
    """With host 'localhost', MySQL tools normally ignore the port and use a local
    socket file. We force TCP, so a wrong port must really fail."""
    out = tmp_path / "x.sql"
    with pytest.raises(DatabaseError, match="Could not connect"):
        adapter_for(my_source_db, db_host="localhost", db_port=1).backup(out)
    assert not out.exists()
