"""Cloud storage on the command line: the new flags, `list`, `restore --latest`,
and a full run through the real command line against an S3 server."""

import re

import pytest
from fakes import FakeAdapter, FakeStorage
from typer.testing import CliRunner

import yourtool.cli as cli
from yourtool.restore import RestorePlan, RestoreResult
from yourtool.storage.base import BackupInfo

runner = CliRunner()

OLD = "d_20261009_100000.dump"
NEW = "d_20261010_100000.dump"


def run(args, env=None, input=None):
    return runner.invoke(cli.app, args, env=env or {}, input=input)


# ------------------------------------------------------ the new flags


def test_config_show_lists_the_s3_settings():
    result = run(
        ["config", "show", "--db-user", "u", "--db-name", "d", "--storage", "s3",
         "--bucket", "my-bucket", "--prefix", "backups", "--region", "eu-west-1",
         "--endpoint-url", "http://localhost:9000"]
    )
    assert result.exit_code == 0, result.output
    for key, value in [("storage_type", "s3"), ("storage_bucket", "my-bucket"),
                       ("storage_prefix", "backups"), ("storage_region", "eu-west-1"),
                       ("storage_endpoint_url", "http://localhost:9000")]:
        assert re.search(rf"{key}\s+= {re.escape(value)}\s+\[cli\]", result.stdout), key


def test_s3_without_a_bucket_is_a_clear_error():
    result = run(["config", "show", "--db-user", "u", "--db-name", "d", "--storage", "s3"])
    assert result.exit_code == 1
    assert "storage_bucket is required" in result.output


def test_settings_come_from_environment_too():
    env = {"YOURTOOL_STORAGE_TYPE": "s3", "YOURTOOL_STORAGE_BUCKET": "env-bucket"}
    result = run(["config", "show", "--db-user", "u", "--db-name", "d"], env=env)
    assert re.search(r"storage_bucket\s+= env-bucket\s+\[env\]", result.stdout)


def test_aws_secret_key_is_never_printed(monkeypatch):
    secret = "AKIA-super-secret-key-value"

    def leaky(config):
        from yourtool.storage import StorageError
        raise StorageError(f"the service said: bad key {secret}")

    monkeypatch.setattr(cli, "run_backup", leaky)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    result = run(
        ["backup", "--db-user", "u", "--db-name", "d"],
        env={"YOURTOOL_DB_PASSWORD": "pw-not-secret", "AWS_SECRET_ACCESS_KEY": secret},
    )
    assert result.exit_code == 1
    assert secret not in result.output and "********" in result.output


# ------------------------------------------------------------- `list`


@pytest.fixture
def storage(monkeypatch):
    st = FakeStorage(files={OLD: b"x", NEW: b"yy", "notes.txt": b"z"})
    monkeypatch.setattr(cli, "get_storage", lambda config: st)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    return st


def test_list_needs_no_database_settings(storage):
    result = run(["list"])
    assert result.exit_code == 0, result.output
    assert "Backups in FakeStorage (newest first)" in result.stdout


def test_list_shows_newest_first_and_hides_foreign_files(storage):
    out = run(["list"]).stdout
    assert out.index(NEW) < out.index(OLD)
    assert "notes.txt" not in out
    assert "2026-10-10 10:00:00 UTC" in out  # the time comes from the file name


def test_list_names_only_for_scripts(storage):
    result = run(["list", "--names-only"])
    assert result.stdout.split() == [NEW, OLD]


def test_list_when_empty(monkeypatch):
    monkeypatch.setattr(cli, "get_storage", lambda config: FakeStorage())
    result = run(["list"])
    assert result.exit_code == 0 and "No backups found" in result.stdout
    assert run(["list", "--names-only"]).stdout == ""


def test_quiet_does_not_hide_the_list(storage):
    assert NEW in run(["list", "--quiet"]).stdout


def test_list_storage_errors_exit_1(monkeypatch):
    from yourtool.storage import StorageError

    class Broken(FakeStorage):
        def list(self):
            raise StorageError("The bucket 'x' does not exist")

    monkeypatch.setattr(cli, "get_storage", lambda config: Broken())
    result = run(["list"])
    assert result.exit_code == 1 and "does not exist" in result.output


def test_list_passes_s3_flags_through(monkeypatch):
    seen = {}

    def spy(config):
        seen["config"] = config
        return FakeStorage()

    monkeypatch.setattr(cli, "get_storage", spy)
    run(["list", "--storage", "s3", "--bucket", "b", "--prefix", "p", "--region", "r",
         "--endpoint-url", "http://x:1"])
    cfg = seen["config"]
    assert (cfg.storage_type, cfg.storage_bucket, cfg.storage_prefix, cfg.storage_region,
            cfg.storage_endpoint_url) == ("s3", "b", "p", "r", "http://x:1")


# ------------------------------------------------------ restore --latest


@pytest.fixture
def restore_world(storage, monkeypatch):
    seen = {}

    def fake_run_restore(config, name, *, clean=False, confirm):
        seen["name"] = name
        plan = RestorePlan(backup_name=name, target_db=config.db_name, host=config.db_host,
                           port=config.db_port, target_exists=False, clean=clean)
        confirm(plan)
        return RestoreResult(backup_name=name, target_db=config.db_name, created_database=True)

    monkeypatch.setattr(cli, "run_restore", fake_run_restore)
    return seen


RESTORE = ["restore", "--db-user", "u", "--target-db", "t", "--no-prompt"]


def test_latest_restores_the_newest_without_any_prompt(restore_world):
    result = run([*RESTORE, "--latest"], env={"YOURTOOL_DB_PASSWORD": "pw"})
    assert result.exit_code == 0, result.output
    assert restore_world["name"] == NEW


def test_latest_with_no_backups(monkeypatch):
    monkeypatch.setattr(cli, "get_storage", lambda config: FakeStorage())
    result = run([*RESTORE, "--latest"])
    assert result.exit_code == 1 and "No backups found" in result.output


def test_file_and_latest_together_is_a_usage_error(restore_world):
    result = run([*RESTORE, "--latest", "--file", OLD])
    assert result.exit_code == 2
    assert "either --file or --latest" in result.output


def test_non_interactive_without_file_or_latest_mentions_both(restore_world):
    result = run(RESTORE)
    assert result.exit_code == 1
    assert "--file" in result.output and "--latest" in result.output


# --------------------- the real thing: command line -> S3 server and back


@pytest.fixture
def real_s3(s3_env, s3_bucket, monkeypatch):
    """Real S3 storage over HTTP; only the database is faked."""
    adapter = FakeAdapter(content=b"-- pretend this is a database dump\n", exists=False)
    monkeypatch.setattr("yourtool.backup.get_adapter", lambda config: adapter)
    monkeypatch.setattr("yourtool.restore.get_adapter", lambda config: adapter)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    flags = ["--storage", "s3", "--bucket", s3_bucket, "--prefix", "backups/demo",
             "--region", "us-east-1", "--endpoint-url", s3_env]
    return adapter, flags, s3_bucket


def test_backup_list_restore_through_the_cli_against_s3(real_s3, s3_client):
    adapter, flags, bucket = real_s3
    env = {"YOURTOOL_DB_PASSWORD": "pw"}
    DB = ["--db-user", "u", "--db-name", "mydb", "--no-prompt"]

    # backup
    result = run(["backup", *DB, *flags], env=env)
    assert result.exit_code == 0, result.output
    assert f"Backup complete: s3://{bucket}/backups/demo/mydb_" in result.stdout

    # it is really in the bucket, under the prefix: the backup and its checksum file
    keys = sorted(o["Key"] for o in s3_client.list_objects_v2(Bucket=bucket)["Contents"])
    assert len(keys) == 2
    assert keys[0].startswith("backups/demo/mydb_") and keys[0].endswith(".dump")
    assert keys[1] == keys[0] + ".sha256"

    # list
    result = run(["list", *flags])
    assert result.exit_code == 0, result.output
    assert f"Backups in s3://{bucket}/backups/demo" in result.stdout
    name = run(["list", "--names-only", *flags]).stdout.strip()
    assert name == keys[0].removeprefix("backups/demo/")

    # restore the latest into a new database; the dump arrives byte for byte
    result = run(["restore", "--db-user", "u", "--target-db", "copy", "--no-prompt",
                  "--latest", *flags], env=env)
    assert result.exit_code == 0, result.output
    assert "created and restored" in result.stdout
    assert adapter.restored_bytes == b"-- pretend this is a database dump\n"


def test_wrong_bucket_through_the_cli_is_a_clear_error(real_s3):
    _, flags, _ = real_s3
    flags[flags.index("--bucket") + 1] = "yt-no-such-bucket-xyz"
    result = run(["backup", "--db-user", "u", "--db-name", "d", "--no-prompt", *flags],
                 env={"YOURTOOL_DB_PASSWORD": "pw"})
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_no_credentials_through_the_cli_is_a_clear_error(real_s3, monkeypatch):
    _, flags, _ = real_s3
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    result = run(["backup", "--db-user", "u", "--db-name", "d", "--no-prompt", *flags],
                 env={"YOURTOOL_DB_PASSWORD": "pw"})
    assert result.exit_code == 1
    assert "AWS credentials" in result.output


def test_gzip_backup_to_s3_survives_verify_and_restore_but_not_tampering(real_s3, s3_client):
    adapter, flags, bucket = real_s3
    env = {"YOURTOOL_DB_PASSWORD": "pw"}
    DB = ["--db-user", "u", "--db-name", "mydb", "--no-prompt"]

    assert run(["backup", *DB, "--compression", "gzip", *flags], env=env).exit_code == 0
    keys = sorted(o["Key"] for o in s3_client.list_objects_v2(Bucket=bucket)["Contents"])
    assert len(keys) == 2 and keys[0].endswith(".dump.gz") and keys[1] == keys[0] + ".sha256"

    # list shows one backup (not the checksum file); verify and restore are happy
    names = run(["list", "--names-only", *flags]).stdout.split()
    assert names == [keys[0].removeprefix("backups/demo/")]
    assert run(["verify", *flags]).exit_code == 0
    adapter.calls.clear()
    assert run(["restore", "--db-user", "u", "--target-db", "copy", "--no-prompt",
                "--latest", *flags], env=env).exit_code == 0
    assert adapter.restored_bytes == b"-- pretend this is a database dump\n"  # decompressed

    # someone replaces the backup object in the bucket
    s3_client.put_object(Bucket=bucket, Key=keys[0], Body=b"not the backup you made")
    result = run(["verify", *flags])
    assert result.exit_code == 1 and "does not match its stored checksum" in result.stdout

    adapter.calls.clear()
    result = run(["restore", "--db-user", "u", "--target-db", "copy2", "--no-prompt",
                  "--latest", *flags], env=env)
    assert result.exit_code == 1
    assert "does not match its stored checksum" in result.output
    assert adapter.calls == []  # the database was never contacted
