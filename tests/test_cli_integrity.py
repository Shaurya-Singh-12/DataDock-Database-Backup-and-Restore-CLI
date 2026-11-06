"""Compression and checksums on the command line: --compression, `verify`,
and what a restore shows."""

import gzip
import re

import pytest
from fakes import FakeAdapter, FakeStorage, sha256_hex, with_checksums
from typer.testing import CliRunner

import yourtool.cli as cli

runner = CliRunner()

DATA = b"INSERT INTO students VALUES (1, 'Alice', 90);\n" * 300
OLD = "d_20261009_100000.dump"
NEW = "d_20261010_100000.dump"
BACKUP = ["backup", "--db-user", "u", "--db-name", "d", "--no-prompt"]
ENV = {"YOURTOOL_DB_PASSWORD": "pw"}


def run(args, env=None, input=None):
    return runner.invoke(cli.app, args, env=env, input=input)


@pytest.fixture
def world(monkeypatch):
    adapter = FakeAdapter(content=DATA, exists=False)
    storage = FakeStorage()
    for module in ("yourtool.backup", "yourtool.restore"):
        monkeypatch.setattr(f"{module}.get_adapter", lambda config: adapter)
        monkeypatch.setattr(f"{module}.get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "get_storage", lambda config: storage)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    return adapter, storage


# ------------------------------------------------------------- the setting


def test_compression_defaults_to_none():
    result = run(["config", "show", "--db-user", "u", "--db-name", "d"])
    assert re.search(r"compression\s+= none\s+\[default\]", result.stdout)


def test_compression_from_flag_environment_and_config_file(tmp_path):
    flag = run(["config", "show", "--db-user", "u", "--db-name", "d", "--compression", "gzip"])
    assert re.search(r"compression\s+= gzip\s+\[cli\]", flag.stdout)

    env = run(["config", "show", "--db-user", "u", "--db-name", "d"],
              env={"YOURTOOL_COMPRESSION": "GZIP"})  # case does not matter
    assert re.search(r"compression\s+= gzip\s+\[env\]", env.stdout)

    cfg = tmp_path / "c.yaml"
    cfg.write_text("database:\n  user: u\n  name: d\nbackup:\n  compression: gzip\n")
    file = run(["config", "show", "--config", str(cfg)])
    assert re.search(r"compression\s+= gzip\s+\[file\]", file.stdout)


def test_unknown_compression_is_a_clear_error():
    result = run(["config", "show", "--db-user", "u", "--db-name", "d", "--compression", "zip"])
    assert result.exit_code == 1 and "Unsupported compression 'zip'" in result.output


# ------------------------------------------------------------ backup output


def test_plain_backup_reports_its_checksum(world):
    adapter, storage = world
    result = run(BACKUP, env=ENV)
    assert result.exit_code == 0, result.output
    assert "Backup complete:" in result.stdout and ".dump " in result.stdout
    assert f"sha256 {sha256_hex(DATA)[:16]}" in result.stdout
    assert "gzip" not in result.stdout


def test_gzip_backup_reports_sizes_and_checksum(world):
    adapter, storage = world
    result = run([*BACKUP, "--compression", "gzip"], env=ENV)
    assert result.exit_code == 0, result.output
    assert ".dump.gz" in result.stdout
    assert re.search(r"gzip: [\d.]+ (B|KB|MB) -> [\d.]+ (B|KB|MB)", result.stdout)
    (stored,) = [n for n in storage.files if n.endswith(".dump.gz")]
    assert f"sha256 {sha256_hex(storage.files[stored])[:16]}" in result.stdout
    assert gzip.decompress(storage.files[stored]) == DATA


def test_quiet_backup_stays_silent(world):
    assert run([*BACKUP, "--compression", "gzip", "-q"], env=ENV).output.strip() == ""


# -------------------------------------------------------------------- list


def test_list_does_not_show_checksum_files(world):
    run(BACKUP, env=ENV)
    storage = world[1]
    assert any(n.endswith(".sha256") for n in storage.files)  # they are stored...
    names = run(["list", "--names-only"]).stdout.split()
    assert len(names) == 1 and not names[0].endswith(".sha256")  # ...but never listed as backups


# ------------------------------------------------------------------ verify


def seed(storage, **files):
    storage.files.update(with_checksums(files))


def test_verify_with_no_options_checks_the_newest(world):
    _, storage = world
    seed(storage, **{OLD: b"old", NEW: b"new"})
    storage.files[OLD] = b"CHANGED"  # the older one is damaged, but only the newest is checked
    result = run(["verify"])
    assert result.exit_code == 0, result.output
    assert f"OK           {NEW}" in result.stdout and OLD not in result.stdout
    assert "Checked 1 backup(s): 1 ok, 0 without checksum, 0 failed." in result.stdout


def test_verify_detects_a_changed_backup(world):
    _, storage = world
    seed(storage, **{NEW: b"new"})
    storage.files[NEW] = b"new, but altered"
    result = run(["verify"])
    assert result.exit_code == 1
    assert f"FAILED       {NEW}" in result.stdout
    assert "does not match its stored checksum" in result.stdout
    assert "1 failed" in result.stdout


def test_verify_reports_missing_checksums_without_failing(world):
    _, storage = world
    storage.files[NEW] = b"made before checksums existed"
    result = run(["verify"])
    assert result.exit_code == 0
    assert "NO CHECKSUM" in result.stdout and "1 without checksum" in result.stdout


def test_verify_all_checks_everything_and_fails_if_any_is_bad(world):
    _, storage = world
    seed(storage, **{OLD: b"old", NEW: b"new", "d_20261011_100000.dump": b"newer"})
    storage.files[OLD] = b"damaged"
    result = run(["verify", "--all"])
    assert result.exit_code == 1
    assert "Checked 3 backup(s): 2 ok, 0 without checksum, 1 failed." in result.stdout
    assert f"FAILED       {OLD}" in result.stdout


def test_verify_a_named_file(world):
    _, storage = world
    seed(storage, **{OLD: b"old", NEW: b"new"})
    result = run(["verify", "--file", OLD])
    assert result.exit_code == 0 and OLD in result.stdout and NEW not in result.stdout


def test_verify_a_missing_file_is_reported_as_failed(world):
    result = run(["verify", "--file", "nope_20261010_100000.dump"])
    assert result.exit_code == 1 and "FAILED" in result.stdout and "not found" in result.stdout


def test_verify_also_checks_inside_gzip_backups(world):
    _, storage = world
    seed(storage, **{NEW + ".gz": gzip.compress(DATA)[:-25]})  # checksum matches the cut-off file
    result = run(["verify"])
    assert result.exit_code == 1 and "gzip" in result.stdout


def test_verify_options_are_mutually_exclusive(world):
    assert run(["verify", "--all", "--latest"]).exit_code == 2
    assert run(["verify", "--file", OLD, "--all"]).exit_code == 2


def test_verify_with_no_backups(world):
    result = run(["verify"])
    assert result.exit_code == 1 and "No backups found" in result.output


def test_verify_quiet_prints_only_failures(world):
    _, storage = world
    seed(storage, **{OLD: b"old", NEW: b"new"})
    assert run(["verify", "--all", "-q"]).stdout.strip() == ""  # all fine: silence
    storage.files[OLD] = b"damaged"
    out = run(["verify", "--all", "-q"])
    assert out.exit_code == 1 and "FAILED" in out.stdout and "OK" not in out.stdout


# ----------------------------------------------------------------- restore


RESTORE = ["restore", "--db-user", "u", "--target-db", "t", "--no-prompt", "--yes"]


def test_restore_summary_shows_what_was_verified(world):
    _, storage = world
    seed(storage, **{NEW: DATA})
    result = run([*RESTORE, "--file", NEW], env=ENV)
    assert result.exit_code == 0, result.output
    assert "Check  : checksum verified" in result.stdout


def test_restore_of_a_tampered_backup_stops_before_touching_the_database(world):
    adapter, storage = world
    seed(storage, **{NEW: DATA})
    storage.files[NEW] = DATA + b"tampered"
    result = run([*RESTORE, "--file", NEW], env=ENV)
    assert result.exit_code == 1
    assert "does not match its stored checksum" in result.output
    assert adapter.calls == []


def test_restore_of_an_old_backup_warns_but_works(world):
    _, storage = world
    storage.files[NEW] = DATA  # no checksum file
    result = run([*RESTORE, "--file", NEW], env=ENV)
    assert result.exit_code == 0, result.output
    assert "No checksum was stored" in result.output
    assert "NO CHECKSUM" in result.stdout


def test_a_backup_made_here_restores_here(world):
    """The whole loop through the command line: backup (gzip) -> verify -> restore."""
    adapter, storage = world
    assert run([*BACKUP, "--compression", "gzip"], env=ENV).exit_code == 0
    assert run(["verify"]).exit_code == 0
    result = run([*RESTORE, "--latest"], env=ENV)
    assert result.exit_code == 0, result.output
    assert adapter.restored_bytes == DATA  # decompressed back to the original
    assert "gzip" in result.stdout
