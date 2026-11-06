"""The restore workflow.

Like backup.py, this knows nothing about the command line. The CLI decides
how to ask the person for confirmation and passes that in as ``confirm``.

Order of events (nothing is changed until the very last step):
  1. download the backup
  2. check it against its checksum  -> refuse if it does not match
  3. decompress it, if it is gzip
  4. ask the database tool whether it is a valid backup
  5. look at the target database
  6. ask for confirmation
  7. create the database (if new) and restore
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from yourtool.compression import decompress_file, is_gzip_name, strip_gzip_suffix
from yourtool.config import Config
from yourtool.databases import DatabaseAdapter, get_adapter
from yourtool.integrity import verify_stored_backup
from yourtool.storage import StorageAdapter, StorageError, get_storage
from yourtool.utils.naming import is_checksum_name

log = logging.getLogger(__name__)


class RestoreCancelled(Exception):
    """The restore was stopped before anything was changed."""


@dataclass(frozen=True)
class RestorePlan:
    """What is about to happen. Shown to the person before they confirm."""

    backup_name: str
    target_db: str
    host: str
    port: int
    target_exists: bool
    clean: bool
    integrity: str = ""  # what was checked about the backup file


@dataclass(frozen=True)
class RestoreResult:
    backup_name: str
    target_db: str
    created_database: bool


def run_restore(
    config: Config,
    backup_name: str,
    *,
    clean: bool = False,
    confirm: Callable[[RestorePlan], bool],
    adapter: DatabaseAdapter | None = None,
    storage: StorageAdapter | None = None,
) -> RestoreResult:
    """Restore ``backup_name`` into the database named in ``config.db_name``.

    ``confirm`` is required on purpose: there is no way to call this
    function and skip the safety check by forgetting about it. It receives
    the plan and returns True to go ahead.
    """
    adapter = adapter or get_adapter(config)
    storage = storage or get_storage(config)

    if is_checksum_name(backup_name):
        raise StorageError(
            f"'{backup_name}' is a checksum file, not a backup. Name the backup it belongs "
            "to instead (the same name without '.sha256')."
        )

    log.info(
        "Starting restore of %s into database '%s' on %s:%s",
        backup_name, config.db_name, config.db_host, config.db_port,
    )
    with tempfile.TemporaryDirectory(prefix="yourtool-") as tmp:
        workdir = Path(tmp)
        downloaded = storage.download(backup_name, workdir / backup_name)
        log.debug("Fetched backup to temporary file %s", downloaded)

        # Is it the file that was stored? A mismatch stops everything here.
        checked = verify_stored_backup(
            storage, backup_name, workdir, local_file=downloaded, check_compression=False
        )
        if checked.status == "ok":
            integrity = f"checksum verified (sha256 {checked.sha256[:16]}…)"
        else:
            integrity = "NO CHECKSUM was stored with this backup, so it could not be verified"

        local_file = downloaded
        if is_gzip_name(backup_name):
            local_file = workdir / strip_gzip_suffix(backup_name)
            decompress_file(downloaded, local_file)
            downloaded.unlink(missing_ok=True)  # free the space
            log.debug("Decompressed to %s", local_file)
            integrity += ", gzip"

        # Everything that can fail cheaply happens BEFORE we change anything.
        adapter.verify_backup(local_file)
        log.info("Backup file verified")
        exists = adapter.database_exists()
        log.info("Target database '%s' %s", config.db_name, "exists" if exists else "does not exist")
        if clean and not exists:
            log.warning("--clean ignored: the target database does not exist yet")

        plan = RestorePlan(
            backup_name=backup_name,
            target_db=config.db_name,
            host=config.db_host,
            port=config.db_port,
            target_exists=exists,
            clean=clean and exists,  # nothing to clean in a brand-new database
            integrity=integrity,
        )
        if not confirm(plan):
            log.info("Restore cancelled at the confirmation step; nothing was changed")
            raise RestoreCancelled("Restore cancelled. Nothing was changed.")

        if not exists:
            log.info("Creating database '%s'", config.db_name)
            adapter.create_database()
        adapter.restore(local_file, clean=plan.clean)

    log.info("Restore finished: %s -> '%s'", backup_name, config.db_name)
    return RestoreResult(
        backup_name=backup_name, target_db=config.db_name, created_database=not exists
    )
