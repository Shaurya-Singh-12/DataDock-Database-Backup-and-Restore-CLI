"""Things that go wrong rarely: unreadable files, bad YAML, disk errors.
Each should produce a clear error message, never a crash."""

import subprocess
import sys
from pathlib import Path

import pytest

from yourtool.config import Config, ConfigError, resolve_config
from yourtool.databases import DatabaseError, PostgresAdapter, get_adapter
from yourtool.storage import LocalStorage, StorageError

REQUIRED = {"db_user": "u", "db_name": "d"}


def config_from_yaml(tmp_path, text):
    path = tmp_path / "c.yaml"
    path.write_text(text)
    return resolve_config(cli=REQUIRED, config_file=path, env={})


# ---------------------------------------------------------------- config


def test_config_path_that_is_a_folder(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read config file"):
        resolve_config(cli=REQUIRED, config_file=tmp_path, env={})


def test_invalid_yaml(tmp_path):
    with pytest.raises(ConfigError, match="Invalid YAML"):
        config_from_yaml(tmp_path, "database: [unclosed")


def test_yaml_top_level_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="mapping at the top level"):
        config_from_yaml(tmp_path, "- one\n- two\n")


def test_empty_yaml_file_is_fine(tmp_path):
    cfg, _ = config_from_yaml(tmp_path, "")
    assert cfg.db_name == "d"


def test_unknown_yaml_section(tmp_path):
    with pytest.raises(ConfigError, match="Unknown section 'databse'"):
        config_from_yaml(tmp_path, "databse:\n  user: x\n")


def test_yaml_section_must_be_a_mapping(tmp_path):
    with pytest.raises(ConfigError, match="Section 'database'.*must be a mapping"):
        config_from_yaml(tmp_path, "database: 5\n")


def test_port_must_be_a_number():
    with pytest.raises(ConfigError, match="db_port must be a number"):
        resolve_config(cli={**REQUIRED, "db_port": "abc"}, env={})


def test_port_zero_is_rejected():
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        resolve_config(cli={**REQUIRED, "db_port": 0}, env={})


def test_unsupported_storage_type():
    with pytest.raises(ConfigError, match="Unsupported storage_type 'ftp'"):
        resolve_config(cli={**REQUIRED, "storage_type": "ftp"}, env={})


# ------------------------------------------------------------- databases


def make_config(**overrides):
    values = dict(
        db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="d",
        storage_type="local", storage_path="./b",
    )
    values.update(overrides)
    return Config(**values)


def test_unknown_database_type_has_no_adapter():
    with pytest.raises(DatabaseError, match="No adapter for database type 'oracle'"):
        get_adapter(make_config(db_type="oracle"))


def test_tool_that_cannot_be_started(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    def cannot_start(*args, **kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    with pytest.raises(DatabaseError, match="Could not run pg_dump: not allowed"):
        PostgresAdapter(make_config()).backup(tmp_path / "x.dump")


# --------------------------------------------------------------- storage


def test_upload_when_backup_folder_is_actually_a_file(tmp_path):
    blocker = tmp_path / "backups"
    blocker.write_text("I am a file, not a folder")
    source = tmp_path / "s.dump"
    source.write_bytes(b"x")
    with pytest.raises(StorageError, match="Could not store backup"):
        LocalStorage(blocker).upload(source, "a.dump")


def test_list_when_folder_cannot_be_read(tmp_path, monkeypatch):
    def unreadable(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    with pytest.raises(StorageError, match="Could not read"):
        LocalStorage(tmp_path).list()


def test_download_to_an_impossible_destination(tmp_path):
    storage = LocalStorage(tmp_path / "store")
    source = tmp_path / "s.dump"
    source.write_bytes(b"x")
    storage.upload(source, "a.dump")

    blocker = tmp_path / "blocker"
    blocker.write_text("a file")
    with pytest.raises(StorageError, match="Could not copy backup"):
        storage.download("a.dump", blocker / "inside" / "copy.dump")


def test_delete_that_fails(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path)
    (tmp_path / "a.dump").write_bytes(b"x")

    def denied(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", denied)
    with pytest.raises(StorageError, match="Could not delete 'a.dump'"):
        storage.delete("a.dump")


# ----------------------------------------------------------- entry point


def test_python_dash_m_yourtool_works():
    result = subprocess.run(
        [sys.executable, "-m", "yourtool", "--version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.startswith("yourtool ")
