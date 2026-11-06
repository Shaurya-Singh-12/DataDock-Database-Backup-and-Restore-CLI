"""The Postgres adapter against a REAL server. Each test uses its own
throwaway database, so nothing depends on databases you already have."""

import shutil
import subprocess

import pytest

from yourtool.config import resolve_config
from yourtool.databases import DatabaseError, PostgresAdapter, get_adapter

pytestmark = pytest.mark.integration


def adapter_for(db_name: str) -> PostgresAdapter:
    # host, port, user and password come from the environment set up by `pg`
    config, _ = resolve_config(cli={"db_name": db_name})
    return get_adapter(config)


def test_real_backup_is_valid_custom_format(source_db, tmp_path):
    out = tmp_path / "real.dump"
    adapter_for(source_db).backup(out)

    assert out.exists() and out.stat().st_size > 0
    listing = subprocess.run(
        [shutil.which("pg_restore"), "--list", str(out)], capture_output=True, text=True
    )
    assert listing.returncode == 0
    assert "students" in listing.stdout


def test_real_backup_of_missing_database_fails_cleanly(pg, tmp_path):
    out = tmp_path / "nope.dump"
    with pytest.raises(DatabaseError):
        adapter_for("yt_does_not_exist").backup(out)
    assert not out.exists()


def test_verify_backup_accepts_real_and_rejects_garbage(source_db, tmp_path):
    adapter = adapter_for(source_db)
    good = tmp_path / "good.dump"
    adapter.backup(good)
    adapter.verify_backup(good)  # no exception

    bad = tmp_path / "bad.dump"
    bad.write_bytes(b"this is not a backup")
    with pytest.raises(DatabaseError, match="not a valid backup"):
        adapter.verify_backup(bad)


def test_database_exists_and_create(pg, source_db, make_database):
    assert adapter_for(source_db).database_exists() is True

    new_name = make_database("yt_new", create=False)
    adapter = adapter_for(new_name)
    assert adapter.database_exists() is False
    adapter.create_database()
    assert adapter.database_exists() is True
    assert pg.database_exists(new_name)  # confirmed by an independent check


@pytest.mark.parametrize("fancy", ['yt_q"uote_{}', "yt space_{}", "yt'apos_{}"])
def test_unusual_database_names_are_quoted_safely(pg, make_database, fancy):
    """Names with quotes or spaces must be treated as plain names, never as SQL."""
    import uuid

    name = make_database(name=fancy.format(uuid.uuid4().hex[:6]), create=False)
    adapter = adapter_for(name)
    assert adapter.database_exists() is False
    adapter.create_database()
    assert adapter.database_exists() is True
    assert pg.database_exists(name)
