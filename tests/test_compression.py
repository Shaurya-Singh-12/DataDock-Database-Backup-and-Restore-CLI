import gzip
import hashlib
import os

import pytest

from yourtool.compression import (
    CompressionError,
    check_gzip,
    compress_file,
    decompress_file,
    is_gzip_name,
    strip_gzip_suffix,
)
from yourtool.storage.base import IntegrityError


def write(path, data):
    path.write_bytes(data)
    return path


@pytest.mark.parametrize(
    "data",
    [b"", b"a", b"\x00\x01\xff\xfe binary \x80", "SELECT 'Zoë, 日本語';\n".encode(), os.urandom(5000)],
    ids=["empty", "one-byte", "binary", "unicode", "random"],
)
def test_round_trip(tmp_path, data):
    src = write(tmp_path / "in.sql", data)
    compress_file(src, tmp_path / "in.sql.gz")
    decompress_file(tmp_path / "in.sql.gz", tmp_path / "out.sql")
    assert (tmp_path / "out.sql").read_bytes() == data


def test_output_is_a_standard_gzip_file(tmp_path):
    """Any other tool (gzip, 7-Zip, Python) can open it."""
    src = write(tmp_path / "in.sql", b"hello world\n" * 100)
    compress_file(src, tmp_path / "in.sql.gz")
    assert gzip.decompress((tmp_path / "in.sql.gz").read_bytes()) == src.read_bytes()


def test_same_input_always_gives_the_same_bytes(tmp_path):
    """No timestamp or file name is stored, so the checksum depends only on the data."""
    src = write(tmp_path / "in.sql", b"data " * 1000)
    compress_file(src, tmp_path / "a.gz")
    os.utime(src, (1_000_000, 1_000_000))  # even a different file date changes nothing
    compress_file(src, tmp_path / "b.gz")
    a = (tmp_path / "a.gz").read_bytes()
    assert a == (tmp_path / "b.gz").read_bytes()
    assert a[4:8] == b"\x00\x00\x00\x00"  # modification time field is zero
    assert a[3] == 0  # no flags: no embedded file name


def test_repetitive_text_shrinks_a_lot(tmp_path):
    src = write(tmp_path / "in.sql", b"INSERT INTO students VALUES (1, 'Alice', 90);\n" * 50_000)
    compress_file(src, tmp_path / "in.sql.gz")
    assert (tmp_path / "in.sql.gz").stat().st_size < src.stat().st_size / 20


def test_large_file_is_streamed_and_intact(tmp_path):
    big = tmp_path / "big.sql"
    with big.open("wb") as handle:
        for i in range(30):  # 30 MB, built piece by piece
            handle.write((f"row {i} " * 100_000).encode())
    compress_file(big, tmp_path / "big.gz")
    decompress_file(tmp_path / "big.gz", tmp_path / "big_out.sql")
    sha = lambda p: hashlib.sha256(p.read_bytes()).digest()  # noqa: E731
    assert sha(big) == sha(tmp_path / "big_out.sql")


@pytest.fixture
def good_gz(tmp_path):
    src = write(tmp_path / "in.sql", os.urandom(20_000))
    compress_file(src, tmp_path / "in.sql.gz")
    return tmp_path / "in.sql.gz"


def test_corrupted_file_is_detected(tmp_path, good_gz):
    data = bytearray(good_gz.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad = write(tmp_path / "bad.gz", bytes(data))
    with pytest.raises(CompressionError, match="not a valid, complete gzip"):
        decompress_file(bad, tmp_path / "out")
    assert not (tmp_path / "out").exists()  # no half-written output left behind
    with pytest.raises(CompressionError):
        check_gzip(bad)


def test_truncated_file_is_detected(tmp_path, good_gz):
    cut = write(tmp_path / "cut.gz", good_gz.read_bytes()[:-50])
    with pytest.raises(CompressionError):
        decompress_file(cut, tmp_path / "out")
    with pytest.raises(CompressionError):
        check_gzip(cut)


def test_non_gzip_file_is_detected(tmp_path):
    bad = write(tmp_path / "x.gz", b"this is plain text, not gzip")
    with pytest.raises(CompressionError):
        decompress_file(bad, tmp_path / "out")


def test_intact_file_passes_the_check(good_gz):
    check_gzip(good_gz)  # no exception


def test_compression_error_is_an_integrity_error(tmp_path):
    with pytest.raises(IntegrityError):
        decompress_file(write(tmp_path / "x.gz", b"nope"), tmp_path / "out")


def test_unwritable_destination(tmp_path):
    src = write(tmp_path / "in.sql", b"data")
    with pytest.raises(CompressionError, match="Could not compress"):
        compress_file(src, tmp_path / "no-such-folder" / "out.gz")


def test_name_helpers():
    assert is_gzip_name("x.dump.gz") and not is_gzip_name("x.dump")
    assert strip_gzip_suffix("x.dump.gz") == "x.dump"
    assert strip_gzip_suffix("x.dump") == "x.dump"
