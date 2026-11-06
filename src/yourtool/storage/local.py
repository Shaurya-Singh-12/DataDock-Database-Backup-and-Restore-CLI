"""Store backups in a folder on this machine."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from yourtool.storage.base import (
    BackupInfo,
    StorageAdapter,
    StorageError,
    validate_backup_name,
)

log = logging.getLogger(__name__)

_PARTIAL_SUFFIX = ".part"


class LocalStorage(StorageAdapter):
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser()

    @staticmethod
    def _check_name(name: str) -> None:
        validate_backup_name(name)
        if name.endswith(_PARTIAL_SUFFIX):  # reserved for files still being written
            raise StorageError(f"Invalid backup name: {name!r}")

    def describe(self) -> str:
        return str(self.directory)

    def upload(self, local_file: Path, name: str) -> str:
        self._check_name(name)
        local_file = Path(local_file)
        if not local_file.is_file():
            raise StorageError(f"File to store does not exist: {local_file}")

        destination = self.directory / name
        partial = destination.with_name(destination.name + _PARTIAL_SUFFIX)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise StorageError(f"A backup named '{name}' already exists.")
            # Copy under a temporary name, then rename. A crash mid-copy can
            # therefore never leave a truncated file that looks like a backup.
            log.debug("Copying %s to %s", local_file, partial)
            shutil.copyfile(local_file, partial)
            os.replace(partial, destination)
            log.debug("Renamed %s to %s", partial.name, destination.name)
        except OSError as exc:
            # Best-effort cleanup of the half-written file. If even that
            # fails, the original problem is the one worth reporting.
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(f"Could not store backup in {self.directory}: {exc}") from exc
        return str(destination)

    def exists(self, name: str) -> bool:
        self._check_name(name)
        return (self.directory / name).is_file()

    def list(self) -> list[BackupInfo]:
        if not self.directory.is_dir():
            return []
        found: list[BackupInfo] = []
        try:
            for entry in self.directory.iterdir():
                if not entry.is_file() or entry.name.endswith(_PARTIAL_SUFFIX):
                    continue
                stat = entry.stat()
                found.append(
                    BackupInfo(
                        name=entry.name,
                        size=stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    )
                )
        except OSError as exc:
            raise StorageError(f"Could not read {self.directory}: {exc}") from exc
        found.sort(key=lambda b: (b.modified, b.name), reverse=True)
        return found

    def download(self, name: str, destination: Path) -> Path:
        self._check_name(name)
        source = self.directory / name
        if not source.is_file():
            raise StorageError(f"Backup '{name}' not found in {self.directory}.")
        destination = Path(destination)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            log.debug("Copying %s to %s", source, destination)
            shutil.copyfile(source, destination)
        except OSError as exc:
            raise StorageError(f"Could not copy backup to {destination}: {exc}") from exc
        return destination

    def delete(self, name: str) -> None:
        self._check_name(name)
        target = self.directory / name
        if not target.is_file():
            raise StorageError(f"Backup '{name}' not found in {self.directory}.")
        try:
            target.unlink()
        except OSError as exc:
            raise StorageError(f"Could not delete '{name}': {exc}") from exc
