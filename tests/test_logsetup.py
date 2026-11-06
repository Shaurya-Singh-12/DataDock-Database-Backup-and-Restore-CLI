import logging
import logging.handlers
import re

from yourtool.utils import logsetup
from yourtool.utils.logsetup import (
    add_secret,
    configure_logging,
    enable_file_logging,
)

log = logging.getLogger("yourtool.test")


def emit_all():
    log.debug("a debug line")
    log.info("an info line")
    log.warning("a warning line")
    log.error("an error line")


# ------------------------------------------------------------ terminal


def test_default_shows_warnings_and_errors_only(capsys):
    configure_logging()
    emit_all()
    err = capsys.readouterr().err
    assert "Warning: a warning line" in err
    assert "Error: an error line" in err
    assert "info line" not in err and "debug line" not in err


def test_verbose_shows_everything(capsys):
    configure_logging(verbose=True)
    emit_all()
    err = capsys.readouterr().err
    assert "[debug] a debug line" in err
    assert "[info] an info line" in err
    assert "Warning: a warning line" in err
    assert "Error: an error line" in err


def test_quiet_shows_errors_only(capsys):
    configure_logging(quiet=True)
    emit_all()
    err = capsys.readouterr().err
    assert "Error: an error line" in err
    assert "warning line" not in err


def test_terminal_output_goes_to_stderr_not_stdout(capsys):
    configure_logging(verbose=True)
    emit_all()
    assert capsys.readouterr().out == ""


def test_reconfiguring_does_not_duplicate_output(capsys):
    configure_logging()
    configure_logging()
    configure_logging()
    log.error("only once")
    assert capsys.readouterr().err.count("only once") == 1


def _raise_and_log():
    try:
        raise ValueError("kaboom")
    except ValueError:
        log.error("it broke", exc_info=True)


def test_traceback_hidden_by_default_shown_when_verbose(capsys):
    configure_logging()
    _raise_and_log()
    assert "Traceback" not in capsys.readouterr().err

    configure_logging(verbose=True)
    _raise_and_log()
    err = capsys.readouterr().err
    assert "Traceback" in err and "kaboom" in err


# ----------------------------------------------------------------- file


def test_file_is_created_with_parent_folders(tmp_path):
    configure_logging()
    target = tmp_path / "deep" / "er" / "run.log"
    assert enable_file_logging(target) is True
    log.info("hello file")
    assert "hello file" in target.read_text()


def test_file_gets_info_by_default_debug_when_verbose(tmp_path):
    configure_logging()
    enable_file_logging(tmp_path / "a.log")
    log.debug("secret-debug-detail")
    log.info("visible-info")
    text = (tmp_path / "a.log").read_text()
    assert "visible-info" in text and "secret-debug-detail" not in text

    configure_logging(verbose=True)
    enable_file_logging(tmp_path / "b.log", verbose=True)
    log.debug("secret-debug-detail")
    assert "secret-debug-detail" in (tmp_path / "b.log").read_text()


def test_file_lines_have_utc_timestamps_and_levels(tmp_path):
    configure_logging()
    enable_file_logging(tmp_path / "a.log")
    log.warning("check format")
    line = (tmp_path / "a.log").read_text().strip()
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z WARNING yourtool\.test: check format$", line
    )


def test_file_log_includes_traceback(tmp_path):
    configure_logging()  # not verbose: terminal hides it, the file keeps it
    enable_file_logging(tmp_path / "a.log")
    _raise_and_log()
    text = (tmp_path / "a.log").read_text()
    assert "Traceback" in text and "kaboom" in text


def test_file_uses_rotation(tmp_path):
    configure_logging()
    enable_file_logging(tmp_path / "a.log")
    handler = next(
        h for h in logging.getLogger("yourtool").handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert handler.maxBytes == logsetup.LOG_MAX_BYTES
    assert handler.backupCount == logsetup.LOG_BACKUP_COUNT


def test_unusable_log_path_warns_but_does_not_crash(tmp_path, capsys):
    configure_logging()
    assert enable_file_logging(tmp_path) is False  # a folder, not a file
    assert "Could not open log file" in capsys.readouterr().err
    log.error("still works")  # logging keeps working


# -------------------------------------------------------------- secrets


def test_secret_is_blanked_on_terminal_and_in_file(tmp_path, capsys):
    configure_logging(verbose=True)
    enable_file_logging(tmp_path / "a.log", verbose=True)
    add_secret("hunter2-pass")

    log.info("connecting with password hunter2-pass now")
    log.error("failed: %s", "bad login for hunter2-pass")

    err = capsys.readouterr().err
    text = (tmp_path / "a.log").read_text()
    assert "hunter2-pass" not in err and "hunter2-pass" not in text
    assert "********" in err and "********" in text


def test_secret_is_blanked_inside_tracebacks(tmp_path, capsys):
    configure_logging(verbose=True)
    enable_file_logging(tmp_path / "a.log")
    add_secret("hunter2-pass")
    try:
        raise RuntimeError("login failed using hunter2-pass")
    except RuntimeError:
        log.error("boom", exc_info=True)

    assert "hunter2-pass" not in capsys.readouterr().err
    assert "hunter2-pass" not in (tmp_path / "a.log").read_text()


def test_very_short_values_are_not_scrubbed(capsys):
    # Scrubbing "12" would wreck every timestamp and port number.
    configure_logging(verbose=True)
    add_secret("12")
    log.info("port 5432 at 12:00")
    assert "port 5432 at 12:00" in capsys.readouterr().err


def test_none_and_empty_secrets_are_ignored(capsys):
    configure_logging(verbose=True)
    add_secret(None)
    add_secret("")
    log.info("nothing to hide")
    assert "nothing to hide" in capsys.readouterr().err


def test_a_secret_survives_reconfiguring_the_logging(capsys):
    """Regression: `schedule run` registered passwords and then reconfigured the logging,
    which used to wipe them, so the password was printed."""
    configure_logging(verbose=True)
    add_secret("hunter2-pass")
    configure_logging(verbose=True, info=True, timestamps=True)  # reconfigured after registering
    log.info("login failed using hunter2-pass")
    err = capsys.readouterr().err
    assert "hunter2-pass" not in err and "********" in err


def test_only_an_explicit_reset_forgets_secrets(capsys):
    configure_logging(verbose=True)
    add_secret("hunter2-pass")
    logsetup.reset_logging()
    configure_logging(verbose=True)
    log.info("hunter2-pass is fine to print now")
    assert "hunter2-pass is fine" in capsys.readouterr().err
