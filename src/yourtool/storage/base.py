"""The contract every storage adapter must follow.

Names passed in here are plain file names (like ``testdb_20261009_143022.dump``),
never paths. Each adapter decides where those names actually live: a folder,
an S3 bucket, and so on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class StorageError(Exception):
    """Raised when a storage operation fails. The message is safe to show users."""


class IntegrityError(StorageError):
    """A stored backup does not match its checksum, or cannot be decompressed:
    it was damaged or changed after it was made."""


def validate_backup_name(name: str) -> None:
    """Backup names are plain file names, never paths. This keeps a name like
    '../../x' from ever reaching outside the backup folder or prefix."""
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        raise StorageError(f"Invalid backup name: {name!r}")


@dataclass(frozen=True)
class BackupInfo:
    name: str
    size: int  # bytes
    modified: datetime  # timezone-aware, UTC


class StorageAdapter(ABC):
    def describe(self) -> str:
        """A short, human-readable location, used in messages."""
        return type(self).__name__

    @abstractmethod
    def upload(self, local_file: Path, name: str) -> str:
        """Store ``local_file`` under ``name``. Returns where it was stored."""

    @abstractmethod
    def exists(self, name: str) -> bool:
        """Is there a stored file called ``name``?"""

    @abstractmethod
    def list(self) -> list[BackupInfo]:
        """All stored backups, newest first."""

    @abstractmethod
    def download(self, name: str, destination: Path) -> Path:
        """Copy the stored backup ``name`` to ``destination``."""

    @abstractmethod
    def delete(self, name: str) -> None:
        """Remove the stored backup ``name``."""
