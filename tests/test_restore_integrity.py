"""What a restore checks before it touches the database."""

import gzip
import logging
from dataclasses import replace

import pytest
from fakes import FakeAdapter, FakeStorage, sha256_hex, with_checksums

from yourtool.compression import CompressionError
from yourtool.config import Config
from yourtool.restore import run_restore
from yourtool.storage import IntegrityError, StorageError

NAME = "mydb_20261009_143022.dump"
DATA = b"-- dump bytes \x00\xff\n" * 40


def config() -> Config:
    return Config(db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="target")


class Recorder:
    """A confirm function that remembers it was asked."""

    def __init__(self):
        self.plans = []

    def __call__(self, plan):
        self.plans.append(plan)
        return True


def restore(files, name=NAME, adapter=None):
    adapter = adapter or FakeAdapter(exists=True)
    confirm = Recorder()
    run_restore(config(), name, confirm=confirm, adapter=adapter, storage=FakeStorage(files=files))
    return adapter, confirm


def gz(data):
    return gzip.compress(data, mtime=0)


# ------------------------------------------------------------ plain backups


def test_a_matching_checksum_lets_the_restore_proceed_and_is_shown_to_the_person():
    adapter, confirm = restore(with_checksums({NAME: DATA}))
    assert adapter.restored_bytes == DATA
    assert "checksum verified" in confirm.plans[0].integrity
    assert sha256_hex(DATA)[:16] in confirm.plans[0].integrity


def test_a_changed_backup_is_refused_before_the_database_is_touched():
    files = with_checksums({NAME: DATA})
    files[NAME] = DATA[:-1] + b"?"
    adapter, confirm = FakeAdapter(exists=True), Recorder()
    with pytest.raises(IntegrityError, match="does not match its stored checksum"):
        run_restore(config(), NAME, confirm=confirm, adapter=adapter, storage=FakeStorage(files=files))
    assert adapter.calls == []  # not even a look at the database
    assert confirm.plans == []  # and the person was never asked to confirm


def test_a_backup_without_a_checksum_still_restores_but_says_so(caplog):
    caplog.set_level(logging.WARNING, logger="yourtool")
    adapter, confirm = restore({NAME: DATA})  # made before checksums existed
    assert adapter.restored_bytes == DATA
    assert confirm.plans[0].integrity.startswith("NO CHECKSUM")
    assert "No checksum was stored" in caplog.text


def test_a_malformed_checksum_file_stops_the_restore():
    adapter = FakeAdapter(exists=True)
    with pytest.raises(IntegrityError, match="not in the expected format"):
        restore({NAME: DATA, NAME + ".sha256": b"<html>not found</html>"}, adapter=adapter)
    assert adapter.calls == []


def test_you_cannot_restore_a_checksum_file():
    with pytest.raises(StorageError, match="is a checksum file, not a backup"):
        restore(with_checksums({NAME: DATA}), name=NAME + ".sha256")


# --------------------------------------------------------- gzip backups


def test_gzip_backup_is_decompressed_before_the_database_tool_sees_it():
    files = with_checksums({NAME + ".gz": gz(DATA)})
    adapter, confirm = restore(files, name=NAME + ".gz")
    assert adapter.restored_bytes == DATA  # the original bytes, not the compressed ones
    assert adapter.local_file.name == NAME  # presented without the .gz
    assert confirm.plans[0].integrity.endswith(", gzip")


def test_gzip_backup_that_was_changed_is_refused_by_its_checksum():
    files = with_checksums({NAME + ".gz": gz(DATA)})
    files[NAME + ".gz"] = gz(DATA + b"tampered")
    adapter = FakeAdapter(exists=True)
    with pytest.raises(IntegrityError, match="does not match"):
        restore(files, name=NAME + ".gz", adapter=adapter)
    assert adapter.calls == []


def test_gzip_backup_that_was_already_broken_when_stored_is_still_caught():
    """The checksum is right for the file as stored; the file itself is cut off."""
    files = with_checksums({NAME + ".gz": gz(DATA)[:-20]})
    adapter = FakeAdapter(exists=True)
    with pytest.raises(CompressionError):
        restore(files, name=NAME + ".gz", adapter=adapter)
    assert adapter.calls == []


def test_gzip_name_on_a_file_that_is_not_gzip_is_caught():
    adapter = FakeAdapter(exists=True)
    with pytest.raises(CompressionError, match="not a valid, complete gzip"):
        restore(with_checksums({NAME + ".gz": b"plain text"}), name=NAME + ".gz", adapter=adapter)
    assert adapter.calls == []


def test_temp_files_are_removed_after_a_gzip_restore():
    adapter, _ = restore(with_checksums({NAME + ".gz": gz(DATA)}), name=NAME + ".gz")
    assert not adapter.local_file.exists()
    assert not adapter.local_file.parent.exists()


def test_confirmation_only_happens_after_every_check_passed():
    """The person sees what was verified, because verification comes first."""
    adapter, confirm = restore(with_checksums({NAME + ".gz": gz(DATA)}), name=NAME + ".gz")
    assert adapter.calls[0] == "verify"  # the database tool's own check ran...
    assert confirm.plans  # ...and only then was the plan shown
