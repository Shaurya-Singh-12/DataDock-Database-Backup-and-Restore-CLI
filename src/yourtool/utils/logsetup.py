"""Logging for the whole program.

Two destinations:

  * the terminal (stderr), shown according to --verbose / --quiet
  * an optional log file, turned on by --log-file / YOURTOOL_LOG_FILE /
    the config file. It is OFF unless you ask for it.

Every message passes through a filter that blanks out any known secret
(the database password). The program never logs the password on purpose;
the filter is a safety net in case a library echoes it in an error message.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path

import typer

LOGGER_NAME = "yourtool"

REDACTED = "********"
# A 1-3 character "secret" would blank out ordinary digits and letters
# everywhere (timestamps, ports), making logs unreadable. Real passwords are
# longer; very short ones are not scrubbed by the safety net.
MIN_SECRET_LENGTH = 4

LOG_MAX_BYTES = 1_000_000  # rotate at about 1 MB ...
LOG_BACKUP_COUNT = 3  # ... and keep this many old files

_secrets: set[str] = set()


def add_secret(value: str | None) -> None:
    """Register a value that must never appear in any log output."""
    if value and len(value) >= MIN_SECRET_LENGTH:
        _secrets.add(value)


def _redact(text: str) -> str:
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _secrets:
            return True
        message = record.getMessage()
        redacted = _redact(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        if record.exc_info:  # tracebacks can contain the secret too
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = _redact(record.exc_text)
        return True


class _TerminalHandler(logging.Handler):
    """Prints log records to stderr, colored by severity, without timestamps."""

    _yourtool_handler = True

    def __init__(self, level: int, show_traceback: bool, timestamps: bool = False) -> None:
        super().__init__(level)
        self.show_traceback = show_traceback
        self.timestamps = timestamps

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.levelno >= logging.ERROR:
                text, color = f"Error: {text}", typer.colors.RED
            elif record.levelno >= logging.WARNING:
                text, color = f"Warning: {text}", typer.colors.YELLOW
            elif record.levelno >= logging.INFO:
                text, color = f"[info] {text}", typer.colors.BRIGHT_BLACK
            else:
                text, color = f"[debug] {text}", typer.colors.BRIGHT_BLACK
            if self.timestamps:  # UTC, like the log file
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created))
                text = f"{stamp} {text}"
            if self.show_traceback and record.exc_info:
                trace = record.exc_text or logging.Formatter().formatException(record.exc_info)
                text += "\n" + trace
            # secho looks up the *current* stderr each time, so this keeps
            # working when the output stream is swapped (as tests do).
            typer.secho(text, err=True, fg=color)
        except Exception:  # never let logging crash the program
            self.handleError(record)


def _remove_our_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_yourtool_handler", False):
            logger.removeHandler(handler)
            handler.close()


def configure_logging(
    *, verbose: bool = False, quiet: bool = False, info: bool = False, timestamps: bool = False
) -> None:
    """(Re)configure the terminal output. Safe to call more than once.

    ``info`` also shows normal progress messages and ``timestamps`` puts the time
    on each line; both are for long-running commands such as the scheduler.
    """
    logger = logging.getLogger(LOGGER_NAME)
    _remove_our_handlers(logger)
    # Registered secrets are deliberately NOT cleared here. A command may reconfigure the
    # terminal after it has registered a password, and forgetting the password at that
    # point would silently stop protecting it. Only reset_logging() clears them.

    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG
    elif info:
        level = logging.INFO
    else:
        level = logging.WARNING

    handler = _TerminalHandler(level, show_traceback=verbose, timestamps=timestamps)
    handler.addFilter(_RedactingFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)  # handlers decide what is actually shown


def enable_file_logging(path: str | Path, *, verbose: bool = False) -> bool:
    """Also write the log to ``path``. Returns False if the file can't be used.

    A broken log path should not stop a backup, so this warns and carries on.
    """
    logger = logging.getLogger(LOGGER_NAME)
    try:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Could not open log file %s: %s. Continuing without a log file.", path, exc)
        return False

    handler._yourtool_handler = True  # type: ignore[attr-defined]
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Timestamps in UTC, like the backup file names.
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.addFilter(_RedactingFilter())
    logger.addHandler(handler)
    return True


def reset_logging() -> None:
    """Remove everything we added. Used by tests."""
    logger = logging.getLogger(LOGGER_NAME)
    _remove_our_handlers(logger)
    _secrets.clear()
    logger.setLevel(logging.NOTSET)
