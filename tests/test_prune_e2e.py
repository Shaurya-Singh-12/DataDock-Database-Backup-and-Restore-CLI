"""prune against real storage: files on disk, and a real S3 server."""

import pytest
from typer.testing import CliRunner

from yourtool.cli import app

runner = CliRunner()


def names(day, db="d", ext=".dump"):
    return f"{db}_202610{day:02d}_100000{ext}"


# -------------------------------------------------------------- real files


def test_prune_on_a_real_folder(tmp_path):
    for day in range(1, 11):
        (tmp_path / names(day)).write_bytes(b"backup bytes")
        (tmp_path / (names(day) + ".sha256")).write_bytes(b"0" * 64 + b"  x\n")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "other_20261001_100000.dump").write_bytes(b"another database")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / names(1)).write_bytes(b"in a sub-folder: not ours to touch")

    result = runner.invoke(
        app, ["prune", "--db-name", "d", "--path", str(tmp_path), "--keep-last", "3", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Deleted 7 backup(s)" in result.stdout

    left = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert left == sorted(
        [names(8), names(9), names(10), names(8) + ".sha256", names(9) + ".sha256",
         names(10) + ".sha256", "notes.txt", "other_20261001_100000.dump"]
    )
    assert (sub / names(1)).exists()


def test_prune_never_removes_the_last_backup_even_with_the_strictest_rule(tmp_path):
    (tmp_path / names(1)).write_bytes(b"only backup")
    runner.invoke(app, ["prune", "--db-name", "d", "--path", str(tmp_path), "--keep-last", "1", "--yes"])
    assert (tmp_path / names(1)).exists()


def test_dry_run_on_a_real_folder_changes_nothing(tmp_path):
    for day in range(1, 6):
        (tmp_path / names(day)).write_bytes(b"x")
    before = sorted(p.name for p in tmp_path.iterdir())
    result = runner.invoke(
        app, ["prune", "--db-name", "d", "--path", str(tmp_path), "--keep-last", "1", "--dry-run"]
    )
    assert result.exit_code == 0 and "Deleting 4" in result.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# ------------------------------------------------------------ a real S3 server


@pytest.fixture
def s3_flags(s3_env, s3_bucket, s3_client):
    for prefix in ("backups/demo", "backups/other"):  # two "folders" with identical file names
        for day in range(1, 7):
            s3_client.put_object(Bucket=s3_bucket, Key=f"{prefix}/{names(day)}", Body=b"x" * 50)
            s3_client.put_object(Bucket=s3_bucket, Key=f"{prefix}/{names(day)}.sha256", Body=b"0" * 64)
    s3_client.put_object(Bucket=s3_bucket, Key="backups/demo/readme.txt", Body=b"mine")
    flags = ["--storage", "s3", "--bucket", s3_bucket, "--prefix", "backups/demo",
             "--region", "us-east-1", "--endpoint-url", s3_env]
    return flags, s3_client, s3_bucket


def keys(client, bucket, prefix):
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    return sorted(o["Key"] for page in pages for o in page.get("Contents", []))


def test_prune_on_s3_deletes_backups_and_checksums_only_under_the_prefix(s3_flags):
    flags, client, bucket = s3_flags
    other_before = keys(client, bucket, "backups/other/")

    result = runner.invoke(app, ["prune", "--db-name", "d", "--keep-last", "2", "--yes", *flags])
    assert result.exit_code == 0, result.output
    assert "Deleted 4 backup(s)" in result.stdout

    assert keys(client, bucket, "backups/demo/") == sorted(
        f"backups/demo/{n}" for n in
        [names(5), names(6), names(5) + ".sha256", names(6) + ".sha256", "readme.txt"]
    )
    assert keys(client, bucket, "backups/other/") == other_before  # a different prefix: untouched


def test_dry_run_on_s3_deletes_nothing(s3_flags):
    flags, client, bucket = s3_flags
    before = keys(client, bucket, "")
    result = runner.invoke(app, ["prune", "--db-name", "d", "--keep-last", "1", "--dry-run", *flags])
    assert result.exit_code == 0 and "Dry run: nothing was deleted." in result.stdout
    assert keys(client, bucket, "") == before


def test_prune_on_s3_with_a_missing_bucket_is_a_clear_error(s3_env):
    result = runner.invoke(
        app, ["prune", "--db-name", "d", "--keep-last", "1", "--yes", "--storage", "s3",
              "--bucket", "yt-no-such-bucket-xyz", "--region", "us-east-1", "--endpoint-url", s3_env]
    )
    assert result.exit_code == 1 and "does not exist" in result.output
