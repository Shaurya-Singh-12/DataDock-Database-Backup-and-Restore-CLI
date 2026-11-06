"""Compression and checksums against a REAL PostgreSQL server. Opt in with
YOURTOOL_INTEGRATION=1 (see the README). Runs for plain and gzip backups."""

import gzip
import hashlib

import pytest
from typer.testing import CliRunner

from yourtool.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def cli(*args):
    return runner.invoke(app, list(args))


@pytest.fixture(params=["none", "gzip"])
def stored(request, pg, source_db, tmp_path):
    result = cli("backup", "--db-name", source_db, "--path", str(tmp_path), "--no-prompt",
                 "--compression", request.param)
    assert result.exit_code == 0, result.output
    (backup,) = [p for p in tmp_path.iterdir() if not p.name.endswith(".sha256")]
    return tmp_path, backup, request.param


def test_files_and_checksum_are_correct(stored):
    path, backup, compression = stored
    assert backup.name.endswith(".dump.gz" if compression == "gzip" else ".dump")
    sidecar = path / (backup.name + ".sha256")
    # computed independently here, with Python's own hashlib
    expected = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert sidecar.read_text().split() == [expected, backup.name]


def test_stored_backup_is_a_genuine_postgres_archive(stored):
    _, backup, compression = stored
    data = backup.read_bytes()
    if compression == "gzip":
        data = gzip.decompress(data)  # an ordinary gzip file
    assert data.startswith(b"PGDMP")  # the PostgreSQL custom-format signature


def test_verify_then_restore_round_trip(stored, pg, make_database):
    path, backup, compression = stored
    assert cli("verify", "--path", str(path)).exit_code == 0

    target = make_database("yt_restore", create=False)
    result = cli("restore", "--target-db", target, "--path", str(path), "--latest", "--no-prompt")
    assert result.exit_code == 0, result.output
    assert "checksum verified" in result.stdout
    assert pg.count(target) == 3


def test_a_single_changed_byte_is_caught_and_nothing_is_restored(stored, pg, make_database):
    path, backup, _ = stored
    data = bytearray(backup.read_bytes())
    data[len(data) // 2] ^= 0x01  # flip one bit, in the middle
    backup.write_bytes(bytes(data))

    result = cli("verify", "--path", str(path))
    assert result.exit_code == 1 and "does not match its stored checksum" in result.stdout

    target = make_database("yt_restore", create=False)
    result = cli("restore", "--target-db", target, "--path", str(path), "--latest", "--no-prompt")
    assert result.exit_code == 1
    assert "does not match its stored checksum" in result.output
    assert pg.database_exists(target) is False  # the target was never even created


def test_a_backup_without_a_checksum_still_restores_with_a_warning(stored, pg, make_database):
    path, backup, _ = stored
    (path / (backup.name + ".sha256")).unlink()  # as if made before checksums existed
    target = make_database("yt_restore", create=False)
    result = cli("restore", "--target-db", target, "--path", str(path), "--latest", "--no-prompt")
    assert result.exit_code == 0, result.output
    assert "No checksum was stored" in result.output and pg.count(target) == 3
