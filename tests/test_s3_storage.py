"""S3 storage. Most tests talk to a real HTTP server that speaks the S3
protocol (moto), so request building, pagination, multipart uploads and the
endpoint settings are genuinely exercised. Error cases that are hard to
trigger on purpose use a fake client instead."""

import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("boto3", reason="install the dev extras: pip install -e '.[dev]'")
pytest.importorskip("moto", reason="install the dev extras: pip install -e '.[dev]'")

from botocore.exceptions import (  # noqa: E402
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)

from yourtool.config import Config  # noqa: E402
from yourtool.storage import S3Storage, StorageError, get_storage  # noqa: E402
from yourtool.storage.s3 import clean_prefix  # noqa: E402


def make_storage(s3_env, bucket, prefix=None, **kw):
    return S3Storage(bucket, prefix=prefix, region="us-east-1", endpoint_url=s3_env, **kw)


@pytest.fixture
def source(tmp_path):
    f = tmp_path / "source.dump"
    f.write_bytes(b"\x00\x01 binary backup \xff\xfe bytes")  # not valid text on purpose
    return f


# ------------------------------------------------------- the test server


def test_the_mock_server_is_reachable_only_from_this_machine(moto_server):
    """Regression test: the server once listened on 0.0.0.0, which Windows cannot
    connect to (every S3 test failed there) and which exposed it to the network."""
    assert moto_server.startswith("http://127.0.0.1:")


# ------------------------------------------------------- real round trip


def test_upload_list_download_delete_round_trip(s3_env, s3_bucket, source, tmp_path):
    storage = make_storage(s3_env, s3_bucket, "backups/prod")
    location = storage.upload(source, "db_20261010_100000.dump")
    assert location == f"s3://{s3_bucket}/backups/prod/db_20261010_100000.dump"

    (info,) = storage.list()
    assert info.name == "db_20261010_100000.dump"
    assert info.size == source.stat().st_size
    assert info.modified.tzinfo is not None and info.modified.utcoffset() == timedelta(0)

    copy = storage.download("db_20261010_100000.dump", tmp_path / "x" / "copy.dump")
    assert copy.read_bytes() == source.read_bytes()

    storage.delete("db_20261010_100000.dump")
    assert storage.list() == []


def test_upload_refuses_to_overwrite(s3_env, s3_bucket, source):
    storage = make_storage(s3_env, s3_bucket)
    storage.upload(source, "a.dump")
    with pytest.raises(StorageError, match="already exists"):
        storage.upload(source, "a.dump")


def test_prefix_is_a_folder_and_is_respected(s3_env, s3_bucket, s3_client, source):
    for key in ("backups/a.dump", "backups/sub/nested.dump", "other/b.dump", "root.dump"):
        s3_client.put_object(Bucket=s3_bucket, Key=key, Body=b"x")
    names = [b.name for b in make_storage(s3_env, s3_bucket, "backups").list()]
    assert names == ["a.dump"]  # not other folders, and not nested ones
    assert [b.name for b in make_storage(s3_env, s3_bucket).list()] == ["root.dump"]


def test_listing_follows_every_page(s3_env, s3_bucket, s3_client):
    """S3 returns results a page at a time; listing must keep asking until it is done.
    (A page size of 4 makes 11 objects take 3 requests, without creating 1000+ objects.)"""
    for i in range(11):
        s3_client.put_object(Bucket=s3_bucket, Key=f"b_{i:02d}.dump", Body=b"x")
    storage = make_storage(s3_env, s3_bucket)
    storage.page_size = 4
    assert sorted(b.name for b in storage.list()) == [f"b_{i:02d}.dump" for i in range(11)]


def test_large_file_uses_multipart_and_arrives_intact(s3_env, s3_bucket, tmp_path):
    big = tmp_path / "big.dump"
    big.write_bytes(os.urandom(20 * 1024 * 1024))  # above boto3's 8 MB multipart threshold
    storage = make_storage(s3_env, s3_bucket)
    storage.upload(big, "big.dump")
    out = storage.download("big.dump", tmp_path / "big_copy.dump")
    assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256(big.read_bytes()).digest()


def test_download_missing_leaves_no_file(s3_env, s3_bucket, tmp_path):
    out = tmp_path / "nope.dump"
    with pytest.raises(StorageError, match="not found"):
        make_storage(s3_env, s3_bucket).download("nope.dump", out)
    assert not out.exists()


def test_delete_missing(s3_env, s3_bucket):
    with pytest.raises(StorageError, match="not found"):
        make_storage(s3_env, s3_bucket).delete("nope.dump")


def test_missing_bucket_message(s3_env, source):
    with pytest.raises(StorageError, match="does not exist"):
        make_storage(s3_env, "yt-no-such-bucket-xyz").upload(source, "a.dump")
    with pytest.raises(StorageError, match="does not exist"):
        make_storage(s3_env, "yt-no-such-bucket-xyz").list()


def test_missing_credentials_message(moto_server, s3_bucket, source, monkeypatch):
    # s3_bucket needed credentials to create the bucket; remove them again, as if
    # the person had never configured any.
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(name)
    with pytest.raises(StorageError, match="AWS credentials"):
        make_storage(moto_server, s3_bucket).upload(source, "a.dump")


@pytest.mark.parametrize("bad", ["../escape.dump", "a/b.dump", "a\\b.dump", "..", ".", ""])
def test_unsafe_names_rejected(s3_env, s3_bucket, source, bad):
    storage = make_storage(s3_env, s3_bucket)
    for action in (lambda: storage.upload(source, bad), lambda: storage.delete(bad)):
        with pytest.raises(StorageError, match="Invalid backup name"):
            action()


def test_upload_of_missing_source(s3_env, s3_bucket, tmp_path):
    with pytest.raises(StorageError, match="does not exist"):
        make_storage(s3_env, s3_bucket).upload(tmp_path / "nope", "a.dump")


# ----------------------------------------------------------- small pieces


@pytest.mark.parametrize(
    "given, expected",
    [(None, ""), ("", ""), ("backups", "backups"), ("/backups/", "backups"),
     ("backups\\prod", "backups/prod"), ("./a//b/", "a/b"), ("  ", "  ")],
)
def test_clean_prefix(given, expected):
    assert clean_prefix(given) == expected


def test_prefix_cannot_climb_out():
    with pytest.raises(StorageError, match=r"\.\."):
        clean_prefix("backups/../../other")


def test_describe():
    assert S3Storage("b", "p/q").describe() == "s3://b/p/q"
    assert S3Storage("b").describe() == "s3://b"


def test_bucket_is_required():
    with pytest.raises(StorageError, match="bucket name is required"):
        S3Storage("")


def test_get_storage_builds_s3_from_config():
    cfg = Config(
        db_type="postgres", db_host="h", db_port=5432, storage_type="s3",
        storage_bucket="my-bucket", storage_prefix="/x/", storage_region="eu-west-1",
        storage_endpoint_url="http://localhost:9000",
    )
    storage = get_storage(cfg)
    assert isinstance(storage, S3Storage)
    assert (storage.bucket, storage.prefix, storage.region, storage.endpoint_url) == (
        "my-bucket", "x", "eu-west-1", "http://localhost:9000",
    )


# --------------------------------------------- how the client is configured


def _capture_client_settings(monkeypatch, seen):
    """Replace boto3's session so we can see how the client would be built."""

    class FakeSession:
        def client(self, *args, **kwargs):
            seen.update(kwargs)
            return object()

    monkeypatch.setattr("boto3.session.Session", FakeSession)


def test_each_storage_object_uses_its_own_session(s3_env):
    a = S3Storage("b", region="us-east-1", endpoint_url=s3_env)._get_client()
    b = S3Storage("b", region="us-east-1", endpoint_url=s3_env)._get_client()
    assert a is not b  # no shared global state


def test_custom_endpoint_gets_compatibility_settings(monkeypatch):
    seen = {}
    _capture_client_settings(monkeypatch, seen)
    S3Storage("b", region="eu-west-1", endpoint_url="http://localhost:9000")._get_client()
    assert seen["endpoint_url"] == "http://localhost:9000" and seen["region_name"] == "eu-west-1"
    cfg = seen["config"]
    assert cfg.s3 == {"addressing_style": "path"}
    assert cfg.request_checksum_calculation == "when_required"
    assert cfg.response_checksum_validation == "when_required"
    assert cfg.connect_timeout == 10 and cfg.retries["max_attempts"] == 3


def test_real_amazon_keeps_the_default_safety_checks(monkeypatch):
    seen = {}
    _capture_client_settings(monkeypatch, seen)
    S3Storage("b", region="eu-west-1")._get_client()
    assert seen["endpoint_url"] is None
    cfg = seen["config"]
    assert cfg.s3 is None  # no path-style override
    assert cfg.request_checksum_calculation != "when_required"  # boto3's stronger default stays on


def test_missing_boto3_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_boto(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("no boto3")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_boto)
    with pytest.raises(StorageError, match=r"yourtool\[s3\]"):
        S3Storage("b")._get_client()


# ------------------------------------------------------- fake-client cases


def client_error(code, status=400, operation="PutObject"):
    return ClientError(
        {"Error": {"Code": code, "Message": "boom"}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeClient:
    def __init__(self, head=None, upload_error=None, head_after=None, content_length=None):
        self.head_results = list(head or [])
        self.upload_error = upload_error
        self.deleted = []
        self.uploaded = []
        self.content_length = content_length

    def head_object(self, **kw):
        result = self.head_results.pop(0) if self.head_results else {"ContentLength": self.content_length}
        if isinstance(result, Exception):
            raise result
        return result

    def upload_file(self, filename, bucket, key):
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append(key)

    def delete_object(self, **kw):
        self.deleted.append(kw["Key"])


def test_write_only_credentials_still_work(source):
    """Credentials that may upload but not read (a good backup practice): the
    existence check and the size check are skipped instead of failing."""
    forbidden = client_error("403", 403, "HeadObject")
    client = FakeClient(head=[forbidden, forbidden])
    location = S3Storage("b", client=client).upload(source, "a.dump")
    assert location == "s3://b/a.dump" and client.uploaded == ["a.dump"]


def test_wrong_stored_size_is_detected_and_removed(source):
    client = FakeClient(head=[client_error("404", 404, "HeadObject"), {"ContentLength": 3}])
    with pytest.raises(StorageError, match="wrong size"):
        S3Storage("b", client=client).upload(source, "a.dump")
    assert client.deleted == ["a.dump"]


@pytest.mark.parametrize(
    "error, expected",
    [
        (client_error("NoSuchBucket", 404), "does not exist"),
        (client_error("AccessDenied", 403), "s3:PutObject"),
        (client_error("InvalidAccessKeyId", 403), "rejected the credentials"),
        (client_error("SignatureDoesNotMatch", 403), "rejected the credentials"),
        (client_error("ExpiredToken", 400), "rejected the credentials"),
        (client_error("PermanentRedirect", 301), "different region"),
        (client_error("AuthorizationHeaderMalformed", 400), "different region"),
        (client_error("SlowDown", 503), "SlowDown|boom"),
        (NoCredentialsError(), "AWS credentials"),
        (EndpointConnectionError(endpoint_url="http://nowhere:1"), "Could not connect"),
        (ParamValidationError(report="Invalid bucket name"), "Invalid S3 setting"),
        # boto3's upload_file wraps S3 errors in a plain exception; the code is only in its text
        (Exception("Failed to upload x to y: An error occurred (NoSuchBucket) when calling the PutObject operation"),
         "does not exist"),
        (Exception("Failed to upload: An error occurred (AccessDenied) when calling the PutObject operation"),
         "Access denied"),
    ],
)
def test_error_messages_are_helpful(source, error, expected):
    # the first HEAD (the "does it exist" check) says "not found", then the upload fails
    client = FakeClient(head=[client_error("404", 404, "HeadObject")], upload_error=error)
    with pytest.raises(StorageError, match=expected):
        S3Storage("b", client=client).upload(source, "a.dump")


def test_listing_normalises_naive_timestamps_and_skips_folder_markers():
    class Paginator:
        def paginate(self, **kw):
            yield {"Contents": [
                {"Key": "p/", "Size": 0, "LastModified": datetime(2026, 1, 1)},          # folder marker
                {"Key": "p/old.dump", "Size": 1, "LastModified": datetime(2026, 1, 1)},   # naive
                {"Key": "p/new.dump", "Size": 2,
                 "LastModified": datetime(2026, 1, 2, tzinfo=timezone(timedelta(hours=5)))},
            ]}

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

    result = S3Storage("b", "p", client=Client()).list()
    assert [b.name for b in result] == ["new.dump", "old.dump"]  # newest first
    assert all(b.modified.utcoffset() == timedelta(0) for b in result)


# ---- exists() (Step 11) ----


def test_exists_true_false_and_respects_the_prefix(s3_env, s3_bucket, s3_client):
    s3_client.put_object(Bucket=s3_bucket, Key="p/a.dump", Body=b"x")
    in_prefix = make_storage(s3_env, s3_bucket, "p")
    assert in_prefix.exists("a.dump") is True
    assert in_prefix.exists("missing.dump") is False
    assert make_storage(s3_env, s3_bucket, "other").exists("a.dump") is False  # a different folder


@pytest.mark.parametrize("bad", ["../x", "a/b", ""])
def test_exists_rejects_unsafe_names(s3_env, s3_bucket, bad):
    with pytest.raises(StorageError, match="Invalid backup name"):
        make_storage(s3_env, s3_bucket).exists(bad)


def test_exists_without_read_permission_is_a_clear_error():
    forbidden = client_error("403", 403, "HeadObject")
    with pytest.raises(StorageError, match="read permission"):
        S3Storage("b", client=FakeClient(head=[forbidden])).exists("a.dump")


def test_exists_cannot_tell_a_missing_bucket_from_a_missing_file(s3_env):
    """S3 answers 'does it exist?' with a HEAD request, whose reply has no body, so a
    missing bucket and a missing file look the same (404). That is how S3 works.
    Real operations still report a missing bucket: listing and uploading say so, and a
    restore downloads the backup first."""
    storage = make_storage(s3_env, "yt-no-such-bucket-xyz")
    assert storage.exists("a.dump") is False
    with pytest.raises(StorageError, match="does not exist"):
        storage.list()
