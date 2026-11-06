"""The MySQL/MariaDB adapter, with the real command-line tools replaced by fakes."""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from yourtool.config import Config
from yourtool.databases import DatabaseError, MySqlAdapter, get_adapter

GOOD_DUMP = (
    "-- MySQL dump 10.13  Distrib 8.0.40, for Linux (x86_64)\n"
    "CREATE TABLE t (id int);\n"
    "-- Dump completed on 2026-10-10 12:00:00\n"
)


def make_config(**overrides) -> Config:
    values = dict(
        db_type="mysql", db_host="dbhost", db_port=3307, db_user="admin", db_name="mydb",
        storage_type="local", storage_path="./backups", db_password="s3cret",
    )
    values.update(overrides)
    return Config(**values)


class ScriptedRun:
    """Stands in for subprocess.run. Answers from a list, one per call, and
    records everything about each call (including the password option file's
    content, which is deleted right after the call)."""

    def __init__(self, *results):
        self.results = list(results) or [(0, "", "")]
        self.calls = []

    def __call__(self, command, **kwargs):
        option = next(
            (p.split("=", 1)[1] for p in command if p.startswith("--defaults-extra-file=")), None
        )
        stdin = kwargs.get("stdin")
        self.calls.append(
            dict(
                command=command,
                kwargs=kwargs,
                option_path=option,
                option_content=Path(option).read_text() if option else None,
                option_mode=stat.S_IMODE(os.stat(option).st_mode) if option else None,
                stdin_content=stdin.read().decode() if stdin else None,
            )
        )
        returncode, stdout, stderr = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if returncode == 0:
            for part in command:
                if part.startswith("--result-file="):
                    Path(part.removeprefix("--result-file=")).write_bytes(GOOD_DUMP.encode())
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    @property
    def sql(self):
        """The SQL text given with --execute in each call."""
        return [
            next((p.removeprefix("--execute=") for p in c["command"] if p.startswith("--execute=")), None)
            for c in self.calls
        ]


def write_dump(path: Path, newline: str = "\n") -> Path:
    """Write the sample dump as exact bytes. (write_text would turn \\n into \\r\\n on
    Windows, silently changing the file.)"""
    path.write_bytes(GOOD_DUMP.replace("\n", newline).encode())
    return path


LINE_ENDINGS = pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["unix", "windows"])


@pytest.fixture
def tools(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def install(monkeypatch, *results):
    fake = ScriptedRun(*results)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


# ------------------------------------------------------------ registration


def test_get_adapter_returns_mysql_for_mysql_type():
    adapter = get_adapter(make_config())
    assert isinstance(adapter, MySqlAdapter)
    assert adapter.file_extension == ".sql"
    assert adapter.password_env_var == "MYSQL_PWD"


# ------------------------------------------------------------------ backup


def test_backup_command(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    out = tmp_path / "out.sql"
    MySqlAdapter(make_config()).backup(out)

    command = fake.calls[0]["command"]
    assert command[0] == "/usr/bin/mysqldump"
    assert command[1].startswith("--defaults-extra-file=")  # must come first
    for expected in (
        "--host=dbhost", "--port=3307", "--user=admin", "--protocol=tcp",
        "--single-transaction", "--routines", "--triggers", "--events",
        "--no-tablespaces", f"--result-file={out}",
    ):
        assert expected in command
    assert command[-1] == "mydb"  # the database name goes last


def test_password_goes_in_a_private_temporary_file_not_the_command(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    MySqlAdapter(make_config()).backup(tmp_path / "out.sql")
    call = fake.calls[0]

    assert not any("s3cret" in part for part in call["command"])
    assert 's3cret' in call["option_content"]
    assert call["option_content"].startswith("[client]")
    if not sys.platform.startswith("win"):
        assert call["option_mode"] == 0o600  # only the owner can read it
    assert not os.path.exists(call["option_path"])  # deleted straight after use
    assert "MYSQL_PWD" not in call["kwargs"]["env"] or call["kwargs"]["env"].get("MYSQL_PWD") != "s3cret"


def test_no_password_means_no_option_file(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    MySqlAdapter(make_config(db_password=None)).backup(tmp_path / "out.sql")
    assert not any("--defaults-extra-file" in part for part in fake.calls[0]["command"])


def test_option_file_escapes_special_characters(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    MySqlAdapter(make_config(db_password='pa"ss\\word#x')).backup(tmp_path / "out.sql")
    assert 'password="pa\\"ss\\\\word#x"' in fake.calls[0]["option_content"]


def test_password_with_line_break_is_rejected_before_anything_runs(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    with pytest.raises(DatabaseError, match="line breaks"):
        MySqlAdapter(make_config(db_password="a\nb")).backup(tmp_path / "out.sql")
    assert fake.calls == []


def test_option_file_removed_even_when_the_tool_cannot_start(monkeypatch, tools, tmp_path):
    seen = {}

    def cannot_start(command, **kwargs):
        seen["path"] = next(p.split("=", 1)[1] for p in command if p.startswith("--defaults-extra-file="))
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    with pytest.raises(DatabaseError, match="Could not run mysqldump"):
        MySqlAdapter(make_config()).backup(tmp_path / "out.sql")
    assert not os.path.exists(seen["path"])


def test_mariadb_tool_names_are_used_when_mysql_names_are_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if name.startswith("mariadb") else None
    )
    fake = install(monkeypatch)
    MySqlAdapter(make_config()).backup(tmp_path / "out.sql")
    assert fake.calls[0]["command"][0] == "/usr/bin/mariadb-dump"


def test_missing_tools_message(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(DatabaseError, match="mysqldump was not found.*mariadb-dump"):
        MySqlAdapter(make_config()).backup(tmp_path / "out.sql")


def test_backup_failure_removes_partial_file(monkeypatch, tools, tmp_path):
    out = tmp_path / "out.sql"
    out.write_text("half a dump")
    install(monkeypatch, (2, "", "mysqldump: something broke"))
    with pytest.raises(DatabaseError, match="something broke"):
        MySqlAdapter(make_config()).backup(out)
    assert not out.exists()


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ('mysqldump: Got error: 1045: "Access denied for user \'admin\'" when trying to connect',
         "YOURTOOL_DB_PASSWORD"),
        ("mysqldump: Got error: 2003: Can't connect to MySQL server on 'dbhost:3307' (111)",
         "Could not connect to the database server"),
        ("mysqldump: Got error: 2005: Unknown MySQL server host 'dbhost' (-2)",
         "Could not connect to the database server"),
        ("mysqldump: Got error: 1049: Unknown database 'mydb' when selecting the database",
         "does not exist"),
        ("ERROR 1044 (42000): Access denied for user 'admin'@'%' to database 'mydb'",
         "not allowed to use that database"),
    ],
)
def test_common_errors_get_plain_language_explanations(monkeypatch, tools, tmp_path, stderr, expected):
    install(monkeypatch, (2, "", stderr))
    with pytest.raises(DatabaseError, match=expected):
        MySqlAdapter(make_config()).backup(tmp_path / "out.sql")


def test_database_names_starting_with_dash_are_refused(monkeypatch, tools, tmp_path):
    fake = install(monkeypatch)
    with pytest.raises(DatabaseError, match="starting with '-'"):
        MySqlAdapter(make_config(db_name="--all-databases")).backup(tmp_path / "out.sql")
    assert fake.calls == []


# ------------------------------------------------------------ verify_backup


@LINE_ENDINGS
def test_verify_accepts_a_finished_mysql_dump(tmp_path, newline):
    MySqlAdapter(make_config()).verify_backup(write_dump(tmp_path / "ok.sql", newline))  # no exception


def test_verify_accepts_a_mariadb_dump(tmp_path):
    f = tmp_path / "ok.sql"
    f.write_bytes(GOOD_DUMP.replace("MySQL dump 10.13", "MariaDB dump 10.19").encode())
    MySqlAdapter(make_config()).verify_backup(f)


def test_verify_rejects_a_truncated_dump(tmp_path):
    f = tmp_path / "cut.sql"
    f.write_text(GOOD_DUMP.split("-- Dump completed")[0])
    with pytest.raises(DatabaseError, match="incomplete"):
        MySqlAdapter(make_config()).verify_backup(f)


@pytest.mark.parametrize("content", [b"this is not a backup", b"PGDMP\x01\x0e\x00binary", b""])
def test_verify_rejects_other_files(tmp_path, content):
    f = tmp_path / "other.sql"
    f.write_bytes(content)
    with pytest.raises(DatabaseError, match="not a valid MySQL/MariaDB backup"):
        MySqlAdapter(make_config()).verify_backup(f)


def test_verify_missing_file(tmp_path):
    with pytest.raises(DatabaseError, match="Could not read"):
        MySqlAdapter(make_config()).verify_backup(tmp_path / "nope.sql")


# --------------------------------------------------- exists / create database


def test_database_exists_true(monkeypatch, tools):
    fake = install(monkeypatch, (0, "1\n", ""))
    assert MySqlAdapter(make_config()).database_exists() is True
    command = fake.calls[0]["command"]
    assert command[0] == "/usr/bin/mysql"
    assert "--database=mydb" in command  # the name is an argument, never pasted into SQL


def test_database_exists_false_on_unknown_database_error(monkeypatch, tools):
    install(monkeypatch, (1, "", "ERROR 1049 (42000): Unknown database 'mydb'"))
    assert MySqlAdapter(make_config()).database_exists() is False


def test_database_exists_raises_on_other_errors(monkeypatch, tools):
    install(monkeypatch, (1, "", "ERROR 1045 (28000): Access denied for user 'admin'@'h'"))
    with pytest.raises(DatabaseError, match="YOURTOOL_DB_PASSWORD"):
        MySqlAdapter(make_config()).database_exists()


def test_create_database_quotes_the_name(monkeypatch, tools):
    fake = install(monkeypatch)
    adapter = MySqlAdapter(make_config(db_name="we`ird; DROP DATABASE x"))
    adapter.create_database()
    assert fake.sql[0] == "CREATE DATABASE `we``ird; DROP DATABASE x`"
    assert adapter._created_database is True


def test_create_database_failure(monkeypatch, tools):
    install(monkeypatch, (1, "", "ERROR 1044 (42000): Access denied for user 'admin'@'%' to database 'mydb'"))
    adapter = MySqlAdapter(make_config())
    with pytest.raises(DatabaseError, match="not allowed"):
        adapter.create_database()
    assert adapter._created_database is False


# ----------------------------------------------------------------- restore


def test_restore_into_non_empty_database_is_refused_without_clean(monkeypatch, tools, tmp_path):
    dump = write_dump(tmp_path / "x.sql")
    fake = install(monkeypatch, (0, "3\n", ""))  # "3 objects already in the database"
    with pytest.raises(DatabaseError, match="not empty"):
        MySqlAdapter(make_config()).restore(dump)
    assert len(fake.calls) == 1  # only the emptiness check ran; the dump was never fed in
    assert fake.calls[0]["stdin_content"] is None


@LINE_ENDINGS
def test_restore_into_empty_database(monkeypatch, tools, tmp_path, newline):
    dump = write_dump(tmp_path / "x.sql", newline)
    fake = install(monkeypatch, (0, "0\n", ""), (0, "", ""))
    MySqlAdapter(make_config()).restore(dump)

    check, restore = fake.calls
    assert "information_schema" in fake.sql[0]
    assert "mydb" not in fake.sql[0]  # DATABASE() is used, so no name is pasted into SQL
    # The dump is streamed in exactly as stored: not one byte changed, whichever line endings.
    assert restore["stdin_content"] == dump.read_bytes().decode()
    assert "--database=mydb" in restore["command"]


@LINE_ENDINGS
def test_restore_with_clean_skips_the_emptiness_check(monkeypatch, tools, tmp_path, newline):
    dump = write_dump(tmp_path / "x.sql", newline)
    fake = install(monkeypatch)
    MySqlAdapter(make_config()).restore(dump, clean=True)
    assert len(fake.calls) == 1
    assert fake.calls[0]["stdin_content"] == dump.read_bytes().decode()


def test_failed_restore_into_new_database_removes_it_again(monkeypatch, tools, tmp_path):
    dump = write_dump(tmp_path / "x.sql")
    fake = install(
        monkeypatch,
        (0, "", ""),                                   # create database
        (0, "0\n", ""),                                # emptiness check
        (1, "", "ERROR 1064 (42000): syntax error"),   # restore fails
        (0, "", ""),                                   # drop the database we created
    )
    adapter = MySqlAdapter(make_config())
    adapter.create_database()
    with pytest.raises(DatabaseError, match="removed again"):
        adapter.restore(dump)
    assert fake.sql[-1] == "DROP DATABASE `mydb`"


def test_failed_restore_into_existing_database_warns_it_may_be_partial(monkeypatch, tools, tmp_path):
    dump = write_dump(tmp_path / "x.sql")
    fake = install(monkeypatch, (1, "", "ERROR 1064 (42000): syntax error"))
    with pytest.raises(DatabaseError, match="partly restored"):
        MySqlAdapter(make_config()).restore(dump, clean=True)
    assert not any(s and s.startswith("DROP DATABASE") for s in fake.sql)  # never drops what it didn't create
