from yourtool.config import Config
from yourtool.storage.base import BackupInfo, IntegrityError, StorageAdapter, StorageError
from yourtool.storage.local import LocalStorage
from yourtool.storage.s3 import S3Storage


def get_storage(config: Config) -> StorageAdapter:
    """Pick the storage adapter named in the config.

    To support a new storage type (GCS, Azure, ...), write an adapter and add a
    branch here.
    """
    if config.storage_type == "local":
        return LocalStorage(config.storage_path)
    if config.storage_type == "s3":
        return S3Storage(
            bucket=config.storage_bucket or "",
            prefix=config.storage_prefix,
            region=config.storage_region,
            endpoint_url=config.storage_endpoint_url,
        )
    raise StorageError(f"No adapter for storage type '{config.storage_type}'.")


__all__ = [
    "BackupInfo",
    "IntegrityError",
    "LocalStorage",
    "S3Storage",
    "StorageAdapter",
    "StorageError",
    "get_storage",
]
