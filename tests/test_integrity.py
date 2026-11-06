import hashlib
import logging
import shutil
import subprocess
import sys

import pytest
from fakes import FakeStorage, sha256_hex, with_checksums

from yourtool.compression import CompressionError, compress_file
from yourtool.integrity import (
    parse_checksum_file,
    sha256_file,
    verify_stored_backup,
    write_checksum_file,
)
from yourtool.storage.base import IntegrityError

NAME = "db_20261010_100000.dump"
DATA = b"the backup bytes \x00\xff"


# ----------------------------------------------------------------- hashing


def test_sha256_known_value(tmp_path):
    f = tmp_path / "abc"
    f.write_bytes(b"abc")
    assert sha256_file(f) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_sha256_of_empty_file(tmp_path):
    f = tmp_path / "empty"
    f.write_bytes(b"")
    assert sha256_file(f) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("size", [1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1, 3 * 1024 * 1024 + 7])
def test_sha256_is_right_across_chunk_boundaries(tmp_path, size):
    f = tmp_path / "x"
    data = bytes(i % 251 for i in range(size))
    f.write_bytes(data)
    assert sha256_file(f) == hashlib.sha256(data).hexdigest()


# --------------------------------------------------------- checksum files


def test_checksum_file_format_is_what_sha256sum_uses(tmp_path):
    f = write_checksum_file(tmp_path / "x.sha256", "a" * 64, "backup.dump")
    assert f.read_bytes() == b"a" * 64 + b"  backup.dump\n"  # exact bytes, LF only, even on Windows


def test_round_trip(tmp_path):
    digest = hashlib.sha256(b"x").hexdigest()
    f = write_checksum_file(tmp_path / "x.sha256", digest, "a b.dump")
    assert parse_checksum_file(f) == digest


@pytest.mark.parametrize(
    "text",
    [
        "{h}  name.dump\n",            # text mode (what sha256sum prints)
        "{h} *name.dump\n",            # binary mode
        "{h}\n",                       # just the hash
        "{h}  name.dump\r\n",          # Windows line endings
        "\n\n{h}  name.dump\n\n",      # blank lines around it
        "{H}  name.dump\n",            # upper-case hex
    ],
)
def test_parse_accepts_the_usual_formats(tmp_path, text):
    h = "ab" * 32
    f = tmp_path / "c.sha256"
    f.write_bytes(text.format(h=h, H=h.upper()).encode())
    assert parse_checksum_file(f) == h


@pytest.mark.parametrize("text", ["", "\n", "notahash  name\n", "abc  name\n", ("zz" * 32) + "  name\n",
                                  "<html>404 not found</html>\n"])
def test_parse_rejects_malformed_files(tmp_path, text):
    f = tmp_path / "c.sha256"
    f.write_bytes(text.encode())
    with pytest.raises(IntegrityError, match="not in the expected format"):
        parse_checksum_file(f)


@pytest.mark.skipif(sys.platform.startswith("win") or shutil.which("sha256sum") is None,
                    reason="needs the sha256sum tool")
def test_our_checksum_file_is_accepted_by_the_real_sha256sum(tmp_path):
    (tmp_path / NAME).write_bytes(DATA)
    write_checksum_file(tmp_path / (NAME + ".sha256"), sha256_hex(DATA), NAME)
    ok = subprocess.run(["sha256sum", "-c", NAME + ".sha256"], cwd=tmp_path, capture_output=True, text=True)
    assert ok.returncode == 0 and "OK" in ok.stdout
    (tmp_path / NAME).write_bytes(DATA + b"x")
    bad = subprocess.run(["sha256sum", "-c", NAME + ".sha256"], cwd=tmp_path, capture_output=True, text=True)
    assert bad.returncode != 0


# ----------------------------------------------- checking a stored backup


def test_matching_backup_is_ok(tmp_path):
    result = verify_stored_backup(FakeStorage(files=with_checksums({NAME: DATA})), NAME, tmp_path)
    assert result.status == "ok" and result.sha256 == sha256_hex(DATA)


def test_changed_backup_is_refused(tmp_path):
    files = with_checksums({NAME: DATA})
    files[NAME] = DATA[:-1] + b"X"  # one byte different
    with pytest.raises(IntegrityError, match="does not match its stored checksum"):
        verify_stored_backup(FakeStorage(files=files), NAME, tmp_path)


def test_missing_checksum_is_reported_not_fatal(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="yourtool")
    result = verify_stored_backup(FakeStorage(files={NAME: DATA}), NAME, tmp_path)
    assert result.status == "unverified"
    assert "No checksum was stored" in caplog.text


def test_malformed_checksum_file_is_refused(tmp_path):
    files = {NAME: DATA, NAME + ".sha256": b"garbage"}
    with pytest.raises(IntegrityError, match="not in the expected format"):
        verify_stored_backup(FakeStorage(files=files), NAME, tmp_path)


def _gzipped(tmp_path, data):
    raw = tmp_path / "raw"
    raw.write_bytes(data)
    compress_file(raw, tmp_path / "x.gz")
    return (tmp_path / "x.gz").read_bytes()


def test_gzip_backup_is_also_checked_for_damage_inside(tmp_path):
    """A checksum can be right for a file that was already broken when it was stored."""
    broken = _gzipped(tmp_path, b"data" * 1000)[:-30]  # cut off, but checksummed as it is
    files = with_checksums({NAME + ".gz": broken})
    with pytest.raises(CompressionError):
        verify_stored_backup(FakeStorage(files=files), NAME + ".gz", tmp_path)
    # a restore decompresses the file anyway, so it skips this second read:
    result = verify_stored_backup(
        FakeStorage(files=files), NAME + ".gz", tmp_path, check_compression=False
    )
    assert result.status == "ok"


def test_intact_gzip_backup_is_ok(tmp_path):
    files = with_checksums({NAME + ".gz": _gzipped(tmp_path, b"data" * 1000)})
    assert verify_stored_backup(FakeStorage(files=files), NAME + ".gz", tmp_path).status == "ok"
