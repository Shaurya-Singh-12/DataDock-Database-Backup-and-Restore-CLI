"""Store backups in Amazon S3, or in any service that speaks the S3 protocol
(MinIO-style servers, Cloudflare R2, Backblaze B2, a local test server, ...).

Credentials are NOT part of this program's settings. boto3 finds them the
standard way: the AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY environment
variables, ~/.aws/credentials, or an IAM role. That keeps secrets out of
config files.

Notes on S3 behaviour:

* An upload is all-or-nothing: an object appears only once it is complete,
  so there is no need for the temporary-file trick the local storage uses.
* There are no real folders. A "prefix" is just the start of every key.
* Credentials limited to writing (no read/list permission) work for uploads;
  the checks that need read access are skipped when S3 says "forbidden".
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yourtool.storage.base import (
    BackupInfo,
    StorageAdapter,
    StorageError,
    validate_backup_name,
)
from yourtool.utils.formatting import format_size

log = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
_ACCESS_DENIED_CODES = {"403", "AccessDenied", "AllAccessDisabled", "Forbidden"}
_BAD_CREDENTIAL_CODES = {
    "InvalidAccessKeyId", "SignatureDoesNotMatch", "InvalidToken", "ExpiredToken",
    "InvalidClientTokenId", "UnrecognizedClientException",
}
_WRONG_REGION_CODES = {
    "PermanentRedirect", "AuthorizationHeaderMalformed", "IllegalLocationConstraintException",
    "301",
}
# boto3's upload_file wraps S3 errors in a plain exception whose text contains the code.
_CODE_IN_TEXT = re.compile(r"An error occurred \((\w+)\)")


def clean_prefix(prefix: str | None) -> str:
    """'/backups\\prod/' -> 'backups/prod'. Refuses '..' so a prefix cannot wander."""
    if not prefix:
        return ""
    parts = [p for p in prefix.replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise StorageError(f"The storage prefix may not contain '..': {prefix!r}")
    return "/".join(parts)


class S3Storage(StorageAdapter):
    #: How many keys to ask S3 for per request (None = S3's own maximum of 1000).
    #: Only tests change this, to exercise paging without creating thousands of objects.
    page_size: int | None = None

    def __init__(
        self,
        bucket: str,
        prefix: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        """``client`` exists so tests can supply their own; normally it is built on demand."""
        if not bucket:
            raise StorageError("An S3 bucket name is required.")
        self.bucket = bucket
        self.prefix = clean_prefix(prefix)
        self.region = region
        self.endpoint_url = endpoint_url
        self._client = client

    # ------------------------------------------------------------ helpers

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}" if self.prefix else f"s3://{self.bucket}"

    def _key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise StorageError(
                "S3 storage needs the 'boto3' package. Install it with:  "
                'pip install "yourtool[s3]"'
            ) from None

        settings: dict[str, Any] = {
            "retries": {"max_attempts": 3, "mode": "standard"},  # rides out brief glitches, fails fast otherwise
            "connect_timeout": 10,
            "read_timeout": 60,
        }
        if self.endpoint_url:
            # A custom endpoint means "not Amazon": use the plain URL layout
            # (host/bucket/key), and don't send the extra integrity headers
            # that newer boto3 versions add by default, which many S3-compatible
            # services reject. Amazon S3 itself keeps the stronger default.
            settings["s3"] = {"addressing_style": "path"}
            settings["request_checksum_calculation"] = "when_required"
            settings["response_checksum_validation"] = "when_required"
        try:
            # Our own session, not boto3's shared default one: no global state,
            # and credentials are looked up fresh for this object.
            self._client = boto3.session.Session().client(
                "s3",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
                config=BotoConfig(**settings),
            )
        except Exception as exc:  # e.g. an invalid region or a missing profile
            raise self._translate(exc, "set up the S3 connection") from exc
        return self._client

    @staticmethod
    def _code(exc: Exception) -> str | None:
        """The S3 error code carried by an exception, if it has one."""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error", {})
            code = error.get("Code")
            if code:
                return str(code)
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status:
                return str(status)
        match = _CODE_IN_TEXT.search(str(exc))
        return match.group(1) if match else None

    def _translate(self, exc: Exception, action: str) -> StorageError:
        """Turn a boto3/botocore exception into a message a person can act on."""
        if isinstance(exc, StorageError):
            return exc
        name = type(exc).__name__
        code = self._code(exc)

        if name in {"NoCredentialsError", "PartialCredentialsError"}:
            return StorageError(
                "No usable AWS credentials were found. Set the AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY environment variables, run 'aws configure', or "
                "use an IAM role."
            )
        if name == "ProfileNotFound":
            return StorageError(f"The AWS profile was not found: {exc}")
        if name in {"EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                    "ConnectionClosedError", "ProxyConnectionError", "SSLError"}:
            where = self.endpoint_url or "Amazon S3"
            return StorageError(
                f"Could not connect to {where}. Check your network connection, the "
                f"endpoint URL and the region.\n\n{exc}"
            )
        if name in {"ParamValidationError", "InvalidRegionError"}:
            return StorageError(f"Invalid S3 setting (check the bucket name and region): {exc}")

        if code == "NoSuchBucket":
            return StorageError(
                f"The bucket '{self.bucket}' does not exist, or is in a different "
                "account or region. Check the bucket name and region."
            )
        if code in _WRONG_REGION_CODES:
            return StorageError(
                f"The bucket '{self.bucket}' is in a different region than the one "
                "being used. Set the region with --region or YOURTOOL_STORAGE_REGION."
            )
        if code in _BAD_CREDENTIAL_CODES:
            return StorageError(
                "The S3 service rejected the credentials (wrong keys, or expired "
                f"temporary credentials).\n\n{exc}"
            )
        if code in _ACCESS_DENIED_CODES:
            return StorageError(
                f"Access denied to {self.describe()}. The credentials need permission to "
                "do this: s3:PutObject to upload, s3:GetObject to download, "
                "s3:ListBucket to list, s3:DeleteObject to delete.\n\n"
                f"({action})"
            )
        if code in _NOT_FOUND_CODES:
            return StorageError(f"Not found in {self.describe()} ({action}).")
        return StorageError(f"Could not {action} ({self.describe()}): {exc}")

    def _exists(self, client: Any, key: str) -> bool | None:
        """True / False, or None when S3 will not say (credentials without read access)."""
        try:
            client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            code = self._code(exc)
            if code in _NOT_FOUND_CODES:
                return False
            if code in _ACCESS_DENIED_CODES:
                log.debug("Cannot check whether %s exists (read access denied)", self._uri(key))
                return None
            raise

    # ------------------------------------------------------------ the interface

    def upload(self, local_file: Path, name: str) -> str:
        validate_backup_name(name)
        local_file = Path(local_file)
        if not local_file.is_file():
            raise StorageError(f"File to store does not exist: {local_file}")

        client = self._get_client()
        key = self._key(name)
        size = local_file.stat().st_size
        started = time.monotonic()
        try:
            if self._exists(client, key) is True:
                raise StorageError(f"A backup named '{name}' already exists in {self.describe()}.")
            log.debug("Uploading %s (%s) to %s", local_file, format_size(size), self._uri(key))
            client.upload_file(str(local_file), self.bucket, key)
        except Exception as exc:
            raise self._translate(exc, "store the backup") from exc
        log.debug("Upload finished in %.2fs", time.monotonic() - started)

        # Confirm the stored object is the size we sent (best effort: this needs read access).
        try:
            stored = client.head_object(Bucket=self.bucket, Key=key).get("ContentLength")
        except Exception as exc:
            if self._code(exc) in _ACCESS_DENIED_CODES:
                log.debug("Skipping the size check (read access denied)")
                return self._uri(key)
            raise self._translate(exc, "verify the uploaded backup") from exc
        if stored != size:
            try:
                client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                pass
            raise StorageError(
                f"The uploaded backup has the wrong size ({stored} bytes stored, {size} sent), "
                "so it was removed. Try again."
            )
        return self._uri(key)

    def exists(self, name: str) -> bool:
        validate_backup_name(name)
        client = self._get_client()
        try:
            found = self._exists(client, self._key(name))
        except Exception as exc:
            raise self._translate(exc, "check for the backup") from exc
        if found is None:  # S3 would not say: the credentials cannot read
            raise StorageError(
                f"Access denied to {self.describe()}: the credentials need read "
                "permission (s3:GetObject) to check for stored files."
            )
        return found

    def list(self) -> list[BackupInfo]:
        client = self._get_client()
        prefix = f"{self.prefix}/" if self.prefix else ""
        found: list[BackupInfo] = []
        log.debug("Listing %s", self.describe())
        try:
            paginator = client.get_paginator("list_objects_v2")
            options = {"PaginationConfig": {"PageSize": self.page_size}} if self.page_size else {}
            for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, **options):
                for obj in page.get("Contents", []):
                    name = obj["Key"][len(prefix):]
                    if not name or "/" in name:  # skip folder markers and nested "folders"
                        continue
                    modified = obj["LastModified"]
                    modified = (
                        modified.replace(tzinfo=timezone.utc)
                        if modified.tzinfo is None
                        else modified.astimezone(timezone.utc)
                    )
                    found.append(BackupInfo(name=name, size=obj["Size"], modified=modified))
        except Exception as exc:
            raise self._translate(exc, "list the backups") from exc
        found.sort(key=lambda b: (b.modified, b.name), reverse=True)
        return found

    def download(self, name: str, destination: Path) -> Path:
        validate_backup_name(name)
        destination = Path(destination)
        key = self._key(name)
        client = self._get_client()
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"Could not create {destination.parent}: {exc}") from exc
        started = time.monotonic()
        try:
            log.debug("Downloading %s to %s", self._uri(key), destination)
            client.download_file(self.bucket, key, str(destination))
        except Exception as exc:
            try:
                destination.unlink(missing_ok=True)  # never leave a partial file behind
            except OSError:
                pass
            if self._code(exc) in _NOT_FOUND_CODES:
                raise StorageError(f"Backup '{name}' not found in {self.describe()}.") from exc
            raise self._translate(exc, "download the backup") from exc
        log.debug("Download finished in %.2fs", time.monotonic() - started)
        return destination

    def delete(self, name: str) -> None:
        validate_backup_name(name)
        key = self._key(name)
        client = self._get_client()
        try:
            if self._exists(client, key) is False:  # S3 deletes "succeed" even for missing keys
                raise StorageError(f"Backup '{name}' not found in {self.describe()}.")
            client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._translate(exc, "delete the backup") from exc
