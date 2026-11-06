"""The contract every database adapter must follow.

The rest of the program only talks to this interface. That is what lets us
add MySQL later without touching the CLI or the storage code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class DatabaseError(Exception):
    """Raised when a database operation fails. The message is safe to show users."""


class DatabaseAdapter(ABC):
    #: File extension for backups this adapter produces (used for naming files).
    file_extension: str

    #: True if the backup format is already compressed (extra gzip then saves little).
    output_is_compressed: bool = False

    #: Environment variable the database's own tools read a password from, if any.
    #: If it is already set, the CLI does not ask for the password again.
    password_env_var: str | None = None

    @abstractmethod
    def backup(self, output_path: Path) -> None:
        """Write a backup of the configured database to ``output_path``."""

    @abstractmethod
    def verify_backup(self, input_path: Path) -> None:
        """Raise DatabaseError if the file is not a readable backup.

        Called before anything is changed, so a corrupt file is caught early.
        """

    @abstractmethod
    def database_exists(self) -> bool:
        """Does the configured (target) database already exist on the server?"""

    @abstractmethod
    def create_database(self) -> None:
        """Create the configured (target) database, empty."""

    @abstractmethod
    def restore(self, input_path: Path, *, clean: bool = False) -> None:
        """Restore the backup at ``input_path`` into the configured database.

        The restore must be all-or-nothing: if it fails, the database is left
        as it was. With ``clean`` objects from the backup replace existing ones.
        """
