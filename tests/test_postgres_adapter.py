import subprocess
from pathlib import Path

import pytest

from yourtool.config import Config
from yourtool.databases import DatabaseError, PostgresAdapter, get_adapter


def make_config(**overrides) -> Config:
    values = dict(
        db_type="postgres",
        db_host="dbhost",
        db_port=5433,
        db_user="admin",
        db_name="mydb",
        storage_type="local",
        storage_path="./backups",
        db_password="s3cret",
    )
    values.update(overrides)
    return Config(**values)


class FakeRun:
    """Stands in for subprocess.run so no real PostgreSQL tools are needed."""

    def __init__(self, returncode=0, stderr="", stdout="", create_file=True):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout
        self.create_file = create_file
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.create_file:
            for part in command:
                if part.startswith("--file="):
                    Path(part.removeprefix("--file=")).write_bytes(b"data")
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def tools_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


# ------------------------------------------------------------------ backup


def test_get_adapter_returns_postgres():
    assert isinstance(get_adapter(make_config()), PostgresAdapter)


def test_command_uses_custom_format_and_settings(monkeypatch, tools_found, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    out = tmp_path / "out.dump"
    PostgresAdapter(make_config()).backup(out)

    command, _ = fake.calls[0]
    assert command[0] == "/usr/bin/pg_dump"
    assert "--format=custom" in command
    assert "--host=dbhost" in command
    assert "--port=5433" in command
    assert "--username=admin" in command
    assert "--dbname=mydb" in command
    assert f"--file={out}" in command
    assert "--no-password" in command


def test_password_is_in_env_not_in_command(monkeypatch, tools_found, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    PostgresAdapter(make_config()).backup(tmp_path / "out.dump")

    command, kwargs = fake.calls[0]
    assert kwargs["env"]["PGPASSWORD"] == "s3cret"
    assert not any("s3cret" in part for part in command)


def test_no_password_leaves_existing_env_alone(monkeypatch, tools_found, tmp_path):
    monkeypatch.setenv("PGPASSWORD", "already-set")
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    PostgresAdapter(make_config(db_password=None)).backup(tmp_path / "out.dump")
    assert fake.calls[0][1]["env"]["PGPASSWORD"] == "already-set"


def test_missing_pg_dump(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(DatabaseError, match="pg_dump was not found"):
        PostgresAdapter(make_config()).backup(tmp_path / "out.dump")


def test_failure_raises_and_removes_partial_file(monkeypatch, tools_found, tmp_path):
    fake = FakeRun(returncode=1, stderr="pg_dump: error: something broke")
    monkeypatch.setattr(subprocess, "run", fake)
    out = tmp_path / "out.dump"
    with pytest.raises(DatabaseError, match="something broke"):
        PostgresAdapter(make_config()).backup(out)
    assert not out.exists()


def test_version_mismatch_gets_friendly_message(monkeypatch, tools_found, tmp_path):
    stderr = "pg_dump: error: aborting because of server version mismatch"
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=stderr))
    with pytest.raises(DatabaseError, match="older than the database server"):
        PostgresAdapter(make_config()).backup(tmp_path / "out.dump")


def test_auth_failure_gets_friendly_message(monkeypatch, tools_found, tmp_path):
    stderr = 'pg_dump: error: connection failed: FATAL: password authentication failed for user "admin"'
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=stderr))
    with pytest.raises(DatabaseError, match="YOURTOOL_DB_PASSWORD"):
        PostgresAdapter(make_config()).backup(tmp_path / "out.dump")


# ----------------------------------------------------------------- restore


def test_restore_command_is_safe_by_default(monkeypatch, tools_found, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    dump = tmp_path / "x.dump"
    PostgresAdapter(make_config()).restore(dump)

    command, kwargs = fake.calls[0]
    assert command[0] == "/usr/bin/pg_restore"
    assert "--single-transaction" in command  # all or nothing
    assert "--no-owner" in command
    assert "--dbname=mydb" in command
    assert "--host=dbhost" in command and "--port=5433" in command
    assert "--no-password" in command
    assert "--clean" not in command
    assert command[-1] == str(dump)
    assert kwargs["env"]["PGPASSWORD"] == "s3cret"
    assert not any("s3cret" in part for part in command)


def test_restore_clean_adds_clean_flags(monkeypatch, tools_found, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    PostgresAdapter(make_config()).restore(tmp_path / "x.dump", clean=True)
    command, _ = fake.calls[0]
    assert "--clean" in command and "--if-exists" in command


def test_restore_clash_gets_hint(monkeypatch, tools_found, tmp_path):
    stderr = 'pg_restore: error: relation "students" already exists'
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=stderr))
    with pytest.raises(DatabaseError, match="--clean"):
        PostgresAdapter(make_config()).restore(tmp_path / "x.dump")


def test_restore_missing_pg_restore(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(DatabaseError, match="pg_restore was not found"):
        PostgresAdapter(make_config()).restore(tmp_path / "x.dump")


def test_verify_backup_uses_list(monkeypatch, tools_found, tmp_path):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    dump = tmp_path / "x.dump"
    PostgresAdapter(make_config()).verify_backup(dump)
    assert fake.calls[0][0] == ["/usr/bin/pg_restore", "--list", str(dump)]


def test_verify_backup_rejects_garbage(monkeypatch, tools_found, tmp_path):
    stderr = "pg_restore: error: input file does not appear to be a valid archive"
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=stderr))
    with pytest.raises(DatabaseError, match="not a valid backup"):
        PostgresAdapter(make_config()).verify_backup(tmp_path / "x.dump")


def test_verify_backup_newer_archive_message(monkeypatch, tools_found, tmp_path):
    stderr = "pg_restore: error: unsupported version (1.16) in file header"
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=1, stderr=stderr))
    with pytest.raises(DatabaseError, match="newer PostgreSQL tools"):
        PostgresAdapter(make_config()).verify_backup(tmp_path / "x.dump")


@pytest.mark.parametrize("stdout, expected", [("1\n", True), ("", False)])
def test_database_exists(monkeypatch, tools_found, stdout, expected):
    fake = FakeRun(stdout=stdout)
    monkeypatch.setattr(subprocess, "run", fake)
    assert PostgresAdapter(make_config()).database_exists() is expected

    command, kwargs = fake.calls[0]
    assert command[0] == "/usr/bin/psql"
    assert "--dbname=postgres" in command  # maintenance database
    assert "name=mydb" in command  # passed as a psql variable
    assert ":'name'" in kwargs["input"]  # psql quotes it; we never splice it into SQL


def test_create_database_quotes_name_as_identifier(monkeypatch, tools_found):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    PostgresAdapter(make_config(db_name='evil"; DROP DATABASE x; --')).create_database()

    command, kwargs = fake.calls[0]
    assert kwargs["input"] == 'CREATE DATABASE :"name";'
    assert 'name=evil"; DROP DATABASE x; --' in command  # data, not SQL


def test_psql_failure_explained(monkeypatch, tools_found):
    stderr = 'psql: error: connection failed: FATAL: password authentication failed for user "admin"'
    monkeypatch.setattr(subprocess, "run", FakeRun(returncode=2, stderr=stderr))
    with pytest.raises(DatabaseError, match="YOURTOOL_DB_PASSWORD"):
        PostgresAdapter(make_config()).database_exists()
