"""Checksums: proof that a stored backup is the same bytes that were backed up.

Each backup is stored together with a small text file, <backup>.sha256, in the
same format as the standard `sha256sum` tool prints ("<hash>  <file name>"), so
it can also be checked by hand:

    sha256sum -c testdb_20261010_100000.dump.gz.sha256

A checksum catches accidental damage (a bad disk, a cut-off copy, a mistaken
edit). It does NOT stop someone who can change both files on purpose; that
needs signing or encryption.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from yourtool.compression import check_gzip, is_gzip_name
from yourtool.storage.base import IntegrityError, StorageAdapter
from yourtool.utils.naming import checksum_name

log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_LINE = re.compile(r"^([0-9a-fA-F]{64})(?:[ \t]+\*?(.*))?$")


@dataclass(frozen=True)
class VerifyResult:
    name: str
    status: str  # "ok" or "unverified" (no checksum was stored)
    sha256: str = ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum_file(destination: Path, hexdigest: str, name: str) -> Path:
    # Written as bytes with "\n" so Windows does not turn it into "\r\n".
    Path(destination).write_bytes(f"{hexdigest}  {name}\n".encode("utf-8"))
    return Path(destination)


def parse_checksum_file(path: Path) -> str:
    """The expected hash (lowercase hex) from a checksum file."""
    text = Path(path).read_bytes().decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match:
            return match.group(1).lower()
        break
    raise IntegrityError(
        f"The checksum file '{Path(path).name}' is not in the expected format "
        "('<64 hex characters>  <file name>')."
    )


def _short(hexdigest: str) -> str:
    return hexdigest[:16] + "…"


def verify_stored_backup(
    storage: StorageAdapter,
    name: str,
    workdir: Path,
    *,
    local_file: Path | None = None,
    check_compression: bool = True,
) -> VerifyResult:
    """Check one stored backup against its checksum (and that a .gz file is intact).

    Raises IntegrityError if it does not match. Returns status "unverified"
    when no checksum was stored (backups made before checksums existed).
    ``local_file`` can be given if the backup was already downloaded. A restore
    passes ``check_compression=False`` because it decompresses the file anyway.
    """
    workdir = Path(workdir)
    if local_file is None:
        local_file = storage.download(name, workdir / name)

    sidecar = checksum_name(name)
    if not storage.exists(sidecar):
        log.warning(
            "No checksum was stored with '%s' (it was made before checksums were added, "
            "or the checksum file was lost), so its integrity cannot be verified.", name,
        )
        result = VerifyResult(name=name, status="unverified")
    else:
        expected = parse_checksum_file(storage.download(sidecar, workdir / sidecar))
        actual = sha256_file(local_file)
        if actual != expected:
            raise IntegrityError(
                f"'{name}' does not match its stored checksum (expected sha256 "
                f"{_short(expected)}, the file has {_short(actual)}). It was damaged or "
                "changed after it was made, so it cannot be trusted."
            )
        log.info("Checksum verified for %s (sha256 %s)", name, actual)
        result = VerifyResult(name=name, status="ok", sha256=actual)

    if check_compression and is_gzip_name(name):
        check_gzip(local_file)
    return result
