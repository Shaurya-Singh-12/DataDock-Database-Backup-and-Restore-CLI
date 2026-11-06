"""The backup workflow: dump the database, optionally compress it, checksum it,
then store both the backup and its checksum file.

This module knows nothing about the command line. The CLI calls
``run_backup``; tests can call it with fake adapters.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yourtool.compression import GZIP_SUFFIX, compress_file
from yourtool.config import Config
from yourtool.databases import DatabaseAdapter, DatabaseError, get_adapter
from yourtool.integrity import sha256_file, write_checksum_file
from yourtool.storage import StorageAdapter, StorageError, get_storage
from yourtool.utils.formatting import format_size
from yourtool.utils.naming import backup_filename, checksum_name

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackupResult:
    name: str  # name of the stored file, e.g. testdb_20261009_143022.dump.gz
    location: str  # where the storage adapter put it
    size: int  # bytes actually stored (after compression, if any)
    original_size: int | None = None  # bytes before compression
    compression: str = "none"
    sha256: str = ""  # checksum of the stored file


def run_backup(
    config: Config,
    *,
    adapter: DatabaseAdapter | None = None,
    storage: StorageAdapter | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Back up the configured database into the configured storage.

    ``adapter``, ``storage`` and ``now`` can be supplied by tests; normally
    they are built from the config and the real clock.
    """
    adapter = adapter or get_adapter(config)
    storage = storage or get_storage(config)
    compression = config.compression
    suffix = GZIP_SUFFIX if compression == "gzip" else ""

    name = backup_filename(config.db_name, adapter.file_extension + suffix, now)
    raw_name = name[: -len(suffix)] if suffix else name
    log.info(
        "Starting backup of '%s' on %s:%s", config.db_name, config.db_host, config.db_port
    )
    if compression != "none" and adapter.output_is_compressed:
        log.warning(
            "This database's backup format is already compressed, so %s will save "
            "little. Continuing as requested.", compression,
        )

    # Everything is built in a private temporary folder first. It is deleted
    # automatically when the block ends, whether we succeed or fail, so a
    # failed run never leaves backup data lying around in the temp folder.
    with tempfile.TemporaryDirectory(prefix="yourtool-") as tmp:
        workdir = Path(tmp)
        raw_file = workdir / raw_name
        log.debug("Dumping to temporary file %s", raw_file)
        adapter.backup(raw_file)

        if not raw_file.is_file() or raw_file.stat().st_size == 0:
            raise DatabaseError("The database tool finished but produced an empty backup.")
        original_size = raw_file.stat().st_size

        if suffix:
            stored_file = workdir / name
            compress_file(raw_file, stored_file)
            raw_file.unlink()  # free the space: only the compressed copy is needed
            log.info(
                "Compressed %s to %s (%s)",
                format_size(original_size), format_size(stored_file.stat().st_size),
                _ratio(original_size, stored_file.stat().st_size),
            )
        else:
            stored_file = raw_file
        size = stored_file.stat().st_size

        # The checksum covers the file exactly as it is stored (compressed, if it is).
        digest = sha256_file(stored_file)
        log.debug("Dump ready (%s, sha256 %s); uploading as %s", format_size(size), digest, name)

        location = storage.upload(stored_file, name)
        log.info("Backup stored: %s (%s)", location, format_size(size))

        # The checksum file goes second: if it exists, the backup it describes is complete.
        try:
            sidecar = write_checksum_file(workdir / checksum_name(name), digest, name)
            storage.upload(sidecar, checksum_name(name))
        except StorageError as exc:
            raise StorageError(
                f"The backup was stored ({location}), but its checksum file could not "
                f"be stored: {exc}\n\nThe backup can still be restored, but without an "
                "integrity check. Run the backup again to get a verified one."
            ) from exc
        log.info("Checksum stored (sha256 %s)", digest)

    return BackupResult(
        name=name,
        location=location,
        size=size,
        original_size=original_size,
        compression=compression,
        sha256=digest,
    )


def _ratio(before: int, after: int) -> str:
    if after <= 0 or after >= before:
        return "no smaller" if after >= before else "empty"
    return f"{before / after:.1f}x smaller"
