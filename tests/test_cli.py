from typer.testing import CliRunner

from yourtool import __version__
from yourtool.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "backup" in result.stdout
    assert "restore" in result.stdout
