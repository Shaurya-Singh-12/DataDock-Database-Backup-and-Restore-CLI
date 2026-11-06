"""Compression and checksums against a REAL MySQL/MariaDB server. Opt in with
YOURTOOL_INTEGRATION_MYSQL=1 (see the README)."""

import gzip
import hashlib
import re

import pytest
from typer.testing import CliRunner

from yourtool.cli import app

pytestmark = pytest.mark.mysql_integration

runner = CliRunner()


def cli(*args):
    return runner.invoke(app, list(args))


@pytest.fixture(params=["none", "gzip"])
def stored(request, my, my_source_db, tmp_path):
    result = cli("backup", "--db-name", my_source_db, "--path", str(tmp_path), "--no-prompt",
                 "--compression", request.param)
    assert result.exit_code == 0, result.output
    (backup,) = [p for p in tmp_path.iterdir() if not p.name.endswith(".sha256")]
    return tmp_path, backup, request.param


def test_files_and_checksum_are_correct(stored):
    path, backup, compression = stored
    assert backup.name.endswith(".sql.gz" if compression == "gzip" else ".sql")
    expected = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert (path / (backup.name + ".sha256")).read_text().split() == [expected, backup.name]


def test_stored_backup_is_readable_sql(stored):
    _, backup, compression = stored
    data = backup.read_bytes()
    text = (gzip.decompress(data) if compression == "gzip" else data).decode("utf-8")
    assert re.search(r"^-- (MySQL|MariaDB) dump", text, re.MULTILINE)
    assert "Zo\u00eb" in text  # non-ASCII data intact through compression


def test_verify_then_restore_round_trip(stored, my, make_my_database):
    path, _, _ = stored
    assert cli("verify", "--path", str(path)).exit_code == 0
    target = make_my_database("yt_restore", create=False)
    result = cli("restore", "--target-db", target, "--path", str(path), "--latest", "--no-prompt")
    assert result.exit_code == 0, result.output
    assert "checksum verified" in result.stdout
    assert my.count(target) == 4
    assert my.sql("SELECT name FROM students WHERE id = 4", target) == "Zo\u00eb"


def test_a_single_changed_byte_is_caught_and_nothing_is_restored(stored, my, make_my_database):
    path, backup, _ = stored
    data = bytearray(backup.read_bytes())
    data[len(data) // 2] ^= 0x01
    backup.write_bytes(bytes(data))

    assert cli("verify", "--path", str(path)).exit_code == 1
    target = make_my_database("yt_restore", create=False)
    result = cli("restore", "--target-db", target, "--path", str(path), "--latest", "--no-prompt")
    assert result.exit_code == 1 and "does not match its stored checksum" in result.output
    assert my.database_exists(target) is False


def test_gzip_makes_a_larger_sql_backup_much_smaller(my, make_my_database, tmp_path):
    db = make_my_database("yt_big")
    my.sql("CREATE TABLE students (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80), grade INT)", db)
    rows = ", ".join(f"('student number {i} of the school', {50 + i % 50})" for i in range(3000))
    my.sql(f"INSERT INTO students (name, grade) VALUES {rows}", db)

    plain, packed = tmp_path / "plain", tmp_path / "packed"
    assert cli("backup", "--db-name", db, "--path", str(plain), "--no-prompt").exit_code == 0
    assert cli("backup", "--db-name", db, "--path", str(packed), "--no-prompt",
               "--compression", "gzip").exit_code == 0
    plain_size = next(p for p in plain.iterdir() if p.name.endswith(".sql")).stat().st_size
    packed_size = next(p for p in packed.iterdir() if p.name.endswith(".sql.gz")).stat().st_size
    assert packed_size < plain_size / 2  # text compresses well
