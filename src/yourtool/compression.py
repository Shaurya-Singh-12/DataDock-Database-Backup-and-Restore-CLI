"""gzip compression for backup files.

Everything streams in 1 MB pieces, so a backup of any size needs no more
memory than a small one.
"""

from __future__ import annotations

import gzip
import shutil
import zlib
from pathlib import Path

from yourtool.storage.base import IntegrityError

GZIP_SUFFIX = ".gz"
_CHUNK = 1024 * 1024
_NOT_GZIP = "is not a valid, complete gzip file (it may be damaged or cut off)"


class CompressionError(IntegrityError):
    """A file could not be compressed or decompressed."""


def is_gzip_name(name: str) -> bool:
    return name.endswith(GZIP_SUFFIX)


def strip_gzip_suffix(name: str) -> str:
    return name[: -len(GZIP_SUFFIX)] if is_gzip_name(name) else name


def compress_file(source: Path, destination: Path, *, level: int = 6) -> None:
    """Write a gzip copy of ``source``.

    The output is the same every time for the same input: no timestamp or file
    name is stored inside it (mtime=0, no name), so a compressed backup's
    checksum depends only on the data.
    """
    try:
        with open(source, "rb") as src, open(destination, "wb") as raw:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=level, mtime=0
            ) as gz:
                shutil.copyfileobj(src, gz, _CHUNK)
    except OSError as exc:
        Path(destination).unlink(missing_ok=True)
        raise CompressionError(f"Could not compress the backup: {exc}") from exc


def decompress_file(source: Path, destination: Path) -> None:
    """Write the decompressed contents of a gzip file."""
    try:
        with gzip.open(source, "rb") as gz, open(destination, "wb") as out:
            shutil.copyfileobj(gz, out, _CHUNK)
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        Path(destination).unlink(missing_ok=True)
        raise CompressionError(f"'{Path(source).name}' {_NOT_GZIP}: {exc}") from exc
    except OSError as exc:
        Path(destination).unlink(missing_ok=True)
        raise CompressionError(f"Could not decompress the backup: {exc}") from exc


def check_gzip(source: Path) -> None:
    """Read a gzip file to the end, to prove it is intact. Writes nothing."""
    try:
        with gzip.open(source, "rb") as gz:
            while gz.read(_CHUNK):
                pass
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise CompressionError(f"'{Path(source).name}' {_NOT_GZIP}: {exc}") from exc
