"""Stand-in adapters so workflow tests need no database and no disk layout."""

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from yourtool.databases import DatabaseAdapter, DatabaseError
from yourtool.storage import BackupInfo, StorageAdapter, StorageError


class FakeAdapter(DatabaseAdapter):
    file_extension = ".dump"

    def __init__(
        self,
        content=b"dump-bytes",
        fail=False,
        write=True,
        exists=True,
        verify_fails=False,
        restore_fails=False,
        compressed_output=False,
    ):
        self.output_is_compressed = compressed_output
        self.content, self.fail, self.write = content, fail, write
        self.exists = exists
        self.verify_fails, self.restore_fails = verify_fails, restore_fails
        self.output_path = None
        self.restored_bytes = None
        self.local_file = None
        self.calls = []  # what happened, in order

    def backup(self, output_path: Path) -> None:
        self.output_path = output_path
        if self.fail:
            raise DatabaseError("database exploded")
        if self.write:
            output_path.write_bytes(self.content)

    def verify_backup(self, input_path: Path) -> None:
        self.calls.append("verify")
        self.local_file = input_path
        if self.verify_fails:
            raise DatabaseError("not a valid backup")

    def database_exists(self) -> bool:
        self.calls.append("exists")
        return self.exists

    def create_database(self) -> None:
        self.calls.append("create")

    def restore(self, input_path: Path, *, clean: bool = False) -> None:
        self.calls.append(("restore", clean))
        self.restored_bytes = Path(input_path).read_bytes()
        if self.restore_fails:
            raise DatabaseError("restore exploded")


class FakeStorage(StorageAdapter):
    def __init__(self, fail=False, files=None):
        self.fail = fail
        self.files = dict(files or {})
        self.uploads = []  # (name, bytes)
        self.deleted = []  # names deleted, in order
        self.fail_delete = set()  # names whose deletion should fail

    def upload(self, local_file, name):
        if self.fail:
            raise StorageError("disk full")
        data = Path(local_file).read_bytes()
        self.uploads.append((name, data))
        self.files[name] = data
        return f"/fake/{name}"

    def exists(self, name):
        return name in self.files

    def list(self):
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [BackupInfo(n, len(d), when) for n, d in self.files.items()]

    def download(self, name, destination):
        if name not in self.files:
            raise StorageError(f"Backup '{name}' not found.")
        destination = Path(destination)
        destination.write_bytes(self.files[name])
        return destination

    def delete(self, name):
        if name in self.fail_delete:
            raise StorageError(f"permission denied for {name}")
        if name not in self.files:
            raise StorageError(f"Backup '{name}' not found.")
        del self.files[name]
        self.deleted.append(name)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def with_checksums(files: dict) -> dict:
    """Return the files plus a correct '<name>.sha256' for each backup in them."""
    result = dict(files)
    for name, data in files.items():
        result[name + ".sha256"] = f"{sha256_hex(data)}  {name}\n".encode()
    return result
