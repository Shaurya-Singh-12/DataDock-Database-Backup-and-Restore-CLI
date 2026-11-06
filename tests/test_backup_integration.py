"""End to end: `yourtool backup` against a REAL PostgreSQL server."""

import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from yourtool.cli import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def test_backup_command_creates_valid_file(source_db, tmp_path):
    result = runner.invoke(
        app, ["backup", "--db-name", source_db, "--path", str(tmp_path), "--no-prompt"]
    )
    assert result.exit_code == 0, result.output

    files = list(tmp_path.glob(f"{source_db}_*.dump"))
    assert len(files) == 1

    listing = subprocess.run(
        [shutil.which("pg_restore"), "--list", str(files[0])], capture_output=True, text=True
    )
    assert listing.returncode == 0
    assert "students" in listing.stdout


def test_backup_command_wrong_database_fails_and_stores_nothing(pg, tmp_path):
    result = runner.invoke(
        app, ["backup", "--db-name", "yt_does_not_exist", "--path", str(tmp_path), "--no-prompt"]
    )
    assert result.exit_code == 1
    assert list(tmp_path.glob("*")) == []


def test_backup_writes_log_file_without_the_password(source_db, tmp_path, pg):
    logfile = tmp_path / "run.log"
    result = runner.invoke(
        app,
        ["backup", "--db-name", source_db, "--path", str(tmp_path / "b"), "--no-prompt",
         "--quiet", "--log-file", str(logfile)],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == ""
    text = logfile.read_text()
    assert "Backup stored" in text
    if pg.password and len(pg.password) >= 4:
        assert pg.password not in text
