import hashlib
from datetime import datetime, timezone

import pytest
from fakes import FakeAdapter, FakeStorage

from yourtool.backup import run_backup
from yourtool.config import Config
from yourtool.databases import DatabaseError
from yourtool.storage import StorageError

NOW = datetime(2026, 10, 9, 14, 30, 22, tzinfo=timezone.utc)


def make_config() -> Config:
    return Config(
        db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="mydb",
        storage_type="local", storage_path="./backups",
    )


def test_success_stores_named_backup():
    storage = FakeStorage()
    result = run_backup(make_config(), adapter=FakeAdapter(), storage=storage, now=NOW)
    assert result.name == "mydb_20261009_143022.dump"
    assert result.location == "/fake/mydb_20261009_143022.dump"
    assert result.size == len(b"dump-bytes")
    # the backup first, then its checksum file (in the standard sha256sum format)
    digest = hashlib.sha256(b"dump-bytes").hexdigest()
    assert storage.uploads == [
        ("mydb_20261009_143022.dump", b"dump-bytes"),
        ("mydb_20261009_143022.dump.sha256", f"{digest}  mydb_20261009_143022.dump\n".encode()),
    ]
    assert result.sha256 == digest and result.compression == "none"


def test_temp_file_is_cleaned_up_after_success():
    adapter = FakeAdapter()
    run_backup(make_config(), adapter=adapter, storage=FakeStorage(), now=NOW)
    assert not adapter.output_path.exists()
    assert not adapter.output_path.parent.exists()


def test_database_failure_skips_upload_and_cleans_up():
    adapter, storage = FakeAdapter(fail=True), FakeStorage()
    with pytest.raises(DatabaseError, match="exploded"):
        run_backup(make_config(), adapter=adapter, storage=storage, now=NOW)
    assert storage.uploads == []
    assert not adapter.output_path.parent.exists()


def test_empty_backup_is_rejected():
    storage = FakeStorage()
    with pytest.raises(DatabaseError, match="empty backup"):
        run_backup(make_config(), adapter=FakeAdapter(content=b""), storage=storage, now=NOW)
    assert storage.uploads == []


def test_missing_output_file_is_rejected():
    with pytest.raises(DatabaseError, match="empty backup"):
        run_backup(make_config(), adapter=FakeAdapter(write=False), storage=FakeStorage(), now=NOW)


def test_storage_failure_propagates_and_cleans_up():
    adapter = FakeAdapter()
    with pytest.raises(StorageError, match="disk full"):
        run_backup(make_config(), adapter=adapter, storage=FakeStorage(fail=True), now=NOW)
    assert not adapter.output_path.parent.exists()


# ---- compression and checksums (Step 11) ----

import gzip  # noqa: E402
import logging  # noqa: E402
from dataclasses import replace  # noqa: E402

from fakes import sha256_hex  # noqa: E402


def gz_config():
    return replace(make_config(), compression="gzip")


SQL = b"INSERT INTO students VALUES (1, 'Alice', 90);\n" * 500


def test_gzip_backup_is_stored_compressed_with_a_checksum_of_the_stored_file():
    storage = FakeStorage()
    result = run_backup(gz_config(), adapter=FakeAdapter(content=SQL), storage=storage, now=NOW)

    stored = dict(storage.uploads)
    assert result.name == "mydb_20261009_143022.dump.gz"
    assert gzip.decompress(stored[result.name]) == SQL  # a standard gzip file
    assert result.compression == "gzip"
    assert result.original_size == len(SQL)
    assert result.size == len(stored[result.name]) < len(SQL) / 10

    # the checksum describes the file as stored (compressed), and names it
    digest = sha256_hex(stored[result.name])
    assert result.sha256 == digest
    assert stored[result.name + ".sha256"] == f"{digest}  {result.name}\n".encode()


def test_backup_is_uploaded_before_its_checksum_file():
    storage = FakeStorage()
    result = run_backup(gz_config(), adapter=FakeAdapter(content=SQL), storage=storage, now=NOW)
    assert [name for name, _ in storage.uploads] == [result.name, result.name + ".sha256"]


def test_same_data_gives_byte_identical_compressed_backups():
    first, second = FakeStorage(), FakeStorage()
    run_backup(gz_config(), adapter=FakeAdapter(content=SQL), storage=first, now=NOW)
    run_backup(gz_config(), adapter=FakeAdapter(content=SQL), storage=second, now=NOW)
    assert first.uploads == second.uploads


def test_no_compression_keeps_the_plain_name_and_reports_no_compression():
    result = run_backup(make_config(), adapter=FakeAdapter(), storage=FakeStorage(), now=NOW)
    assert result.name.endswith(".dump") and result.compression == "none"
    assert result.original_size == result.size


def test_gzip_leaves_nothing_in_the_temp_folder():
    adapter = FakeAdapter(content=SQL)
    run_backup(gz_config(), adapter=adapter, storage=FakeStorage(), now=NOW)
    assert not adapter.output_path.parent.exists()


def test_empty_dump_is_rejected_before_compressing():
    storage = FakeStorage()
    with pytest.raises(DatabaseError, match="empty backup"):
        run_backup(gz_config(), adapter=FakeAdapter(content=b""), storage=storage, now=NOW)
    assert storage.uploads == []


def test_warns_when_gzip_is_pointless_because_the_format_is_already_compressed(caplog):
    caplog.set_level(logging.WARNING, logger="yourtool")
    run_backup(gz_config(), adapter=FakeAdapter(content=SQL, compressed_output=True),
               storage=FakeStorage(), now=NOW)
    assert "already compressed" in caplog.text


def test_no_warning_for_plain_text_formats_or_when_not_compressing(caplog):
    caplog.set_level(logging.WARNING, logger="yourtool")
    run_backup(gz_config(), adapter=FakeAdapter(content=SQL), storage=FakeStorage(), now=NOW)
    run_backup(make_config(), adapter=FakeAdapter(content=SQL, compressed_output=True),
               storage=FakeStorage(), now=NOW)
    assert "already compressed" not in caplog.text


class ChecksumRejectingStorage(FakeStorage):
    def upload(self, local_file, name):
        if name.endswith(".sha256"):
            raise StorageError("disk full")
        return super().upload(local_file, name)


def test_if_only_the_checksum_cannot_be_stored_the_backup_is_kept_and_you_are_told():
    storage = ChecksumRejectingStorage()
    with pytest.raises(StorageError, match="backup was stored.*checksum file could not"):
        run_backup(make_config(), adapter=FakeAdapter(), storage=storage, now=NOW)
    assert "mydb_20261009_143022.dump" in storage.files  # still there, still restorable
