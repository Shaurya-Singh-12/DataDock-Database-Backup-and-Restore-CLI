"""Backup file names: <database>_<YYYYMMDD_HHMMSS><extension>, time in UTC.

Example: testdb_20261009_143022.dump

UTC means the names sort chronologically, never change meaning when
daylight saving starts or ends, and stay consistent across machines.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

#: A checksum file is stored next to each backup: <backup name> + this suffix.
CHECKSUM_SUFFIX = ".sha256"

_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-]")
_FILENAME_RE = re.compile(
    r"^(?P<db>.+)_(?P<ts>\d{8}_\d{6})(?P<ext>\.[A-Za-z0-9.]+)$"
)


def sanitize_db_name(db_name: str) -> str:
    """How a database name appears inside backup file names."""
    return _UNSAFE_CHARS.sub("-", db_name).strip("-.") or "database"


def backup_filename(
    db_name: str, extension: str, now: datetime | None = None
) -> str:
    """Build a backup file name. ``now`` exists so tests can fix the clock."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("'now' must be timezone-aware (use UTC).")
    stamp = now.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)
    # Keep file names safe on every operating system. Letters, digits, dots,
    # hyphens and underscores are kept as they are, so "college_grades" stays
    # "college_grades". (Underscores are safe: the parser below finds the
    # timestamp from the END of the name, so they cannot confuse it.)
    safe_db = sanitize_db_name(db_name)
    if not extension.startswith("."):
        extension = "." + extension
    return f"{safe_db}_{stamp}{extension}"


def parse_backup_filename(filename: str) -> tuple[str, datetime] | None:
    """Return (database name, UTC time) or None if it isn't one of our names."""
    if is_checksum_name(filename):  # a checksum file belongs to a backup; it is not one
        return None
    match = _FILENAME_RE.match(filename)
    if match is None:
        return None
    try:
        when = datetime.strptime(match["ts"], _TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return match["db"], when.replace(tzinfo=timezone.utc)


def checksum_name(backup_name: str) -> str:
    """testdb_20261010_100000.dump.gz -> testdb_20261010_100000.dump.gz.sha256"""
    return backup_name + CHECKSUM_SUFFIX


def is_checksum_name(name: str) -> bool:
    return name.endswith(CHECKSUM_SUFFIX)
