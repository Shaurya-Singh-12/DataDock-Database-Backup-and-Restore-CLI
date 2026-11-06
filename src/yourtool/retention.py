"""Retention: deciding which old backups to keep, and deleting the rest.

The rules work like the ones in tools such as restic. A backup is KEPT if ANY
rule keeps it:

  keep_last N     the N most recent backups
  keep_daily N    the newest backup of each of the N most recent days that have one
  keep_weekly N   ... of each of the N most recent weeks (ISO weeks) that have one
  keep_monthly N  ... of each of the N most recent months that have one

For example "keep_last 3, keep_daily 7, keep_monthly 6" keeps the latest three
backups, one per day for the last week of days you made backups, and one per
month for half a year. Everything else is deleted.

Safety built into the design:

* The newest backup of a database is never deleted (every rule keeps it).
* Only files this tool named are ever considered (<db>_<timestamp>.<ext>).
* Each database is judged separately, so one database's rules can never
  delete another's backups.
* A backup's checksum file is deleted with it, backup first, so a crash can
  never leave a backup without its checksum.
* The decision is made by plan_retention(), a plain function with no side
  effects, so it can be shown to the person (and tested) before anything is deleted.

Times come from the UTC timestamp in the file name, not from file dates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from yourtool.storage.base import BackupInfo, StorageAdapter, StorageError
from yourtool.utils.formatting import format_size
from yourtool.utils.naming import (
    checksum_name,
    is_checksum_name,
    parse_backup_filename,
    sanitize_db_name,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Policy:
    keep_last: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None

    def is_empty(self) -> bool:
        return not any((self.keep_last, self.keep_daily, self.keep_weekly, self.keep_monthly))

    def describe(self) -> str:
        parts = [
            f"{label} {value}"
            for label, value in (
                ("last", self.keep_last),
                ("daily", self.keep_daily),
                ("weekly", self.keep_weekly),
                ("monthly", self.keep_monthly),
            )
            if value
        ]
        return ", ".join(parts)


# (reason label, Policy field, which period a time falls in)
_PERIOD_RULES: list[tuple[str, str, Callable[[datetime], tuple]]] = [
    ("daily", "keep_daily", lambda d: (d.year, d.month, d.day)),
    ("weekly", "keep_weekly", lambda d: tuple(d.isocalendar())[:2]),
    ("monthly", "keep_monthly", lambda d: (d.year, d.month)),
]


@dataclass(frozen=True)
class Backup:
    """One stored backup and, if there is one, its checksum file."""

    name: str
    database: str  # as it appears in the file name
    when: datetime  # UTC, from the file name
    size: int
    checksum: str | None = None  # name of the checksum file, if it exists
    checksum_size: int = 0


@dataclass(frozen=True)
class Decision:
    backup: Backup
    keep: bool
    reasons: tuple[str, ...] = ()  # which rules kept it


def plan_retention(backups: list[Backup], policy: Policy) -> list[Decision]:
    """Decide, for backups of ONE database, which to keep. Newest first.

    This function only decides; it deletes nothing.
    """
    if policy.is_empty():
        raise ValueError("A retention policy needs at least one rule.")

    ordered = sorted(backups, key=lambda b: (b.when, b.name), reverse=True)
    reasons: dict[str, list[str]] = {b.name: [] for b in ordered}

    if policy.keep_last:
        for backup in ordered[: policy.keep_last]:
            reasons[backup.name].append("last")

    for label, attribute, period_of in _PERIOD_RULES:
        limit = getattr(policy, attribute)
        if not limit:
            continue
        seen: set[tuple] = set()
        for backup in ordered:  # newest first: the first one in a period is its newest
            period = period_of(backup.when)
            if period in seen:
                continue
            seen.add(period)
            if len(seen) > limit:
                break  # every later period is older still
            reasons[backup.name].append(label)

    return [Decision(b, bool(reasons[b.name]), tuple(reasons[b.name])) for b in ordered]


@dataclass(frozen=True)
class PrunePlan:
    decisions: dict[str, list[Decision]] = field(default_factory=dict)  # per database
    orphan_checksums: tuple[str, ...] = ()  # checksum files whose backup is gone

    def to_delete(self) -> list[Backup]:
        return [d.backup for group in self.decisions.values() for d in group if not d.keep]

    def kept_count(self) -> int:
        return sum(1 for group in self.decisions.values() for d in group if d.keep)

    def total_count(self) -> int:
        return sum(len(group) for group in self.decisions.values())


def build_plan(
    listing: list[BackupInfo], *, database: str | None, policy: Policy
) -> PrunePlan:
    """Work out the plan from what is in storage.

    ``database`` limits the plan to one database (as named in the config);
    ``None`` means every database found.
    """
    wanted = sanitize_db_name(database) if database is not None else None
    by_name = {info.name: info for info in listing}

    grouped: dict[str, list[Backup]] = {}
    for info in listing:
        if is_checksum_name(info.name):
            continue
        parsed = parse_backup_filename(info.name)
        if parsed is None:
            continue  # not one of ours: never touched
        db, when = parsed
        if wanted is not None and db != wanted:
            continue  # another database: never touched
        sidecar = by_name.get(checksum_name(info.name))
        grouped.setdefault(db, []).append(
            Backup(
                name=info.name,
                database=db,
                when=when,
                size=info.size,
                checksum=sidecar.name if sidecar else None,
                checksum_size=sidecar.size if sidecar else 0,
            )
        )

    orphans = []
    for name in by_name:
        if not is_checksum_name(name):
            continue
        backup_name = name[: -len(checksum_name(""))]
        if backup_name in by_name:
            continue
        parsed = parse_backup_filename(backup_name)
        if parsed is None or (wanted is not None and parsed[0] != wanted):
            continue
        orphans.append(name)

    decisions = {db: plan_retention(items, policy) for db, items in sorted(grouped.items())}
    return PrunePlan(decisions=decisions, orphan_checksums=tuple(sorted(orphans)))


@dataclass
class PruneResult:
    deleted: list[Backup] = field(default_factory=list)
    failed: list[tuple[Backup, str]] = field(default_factory=list)
    freed: int = 0


def execute_prune(storage: StorageAdapter, doomed: list[Backup]) -> PruneResult:
    """Delete the given backups. A failure on one does not stop the others."""
    result = PruneResult()
    for backup in doomed:
        try:
            storage.delete(backup.name)  # the backup first...
        except StorageError as exc:
            log.error("Could not delete %s: %s", backup.name, exc)
            result.failed.append((backup, str(exc)))
            continue
        result.deleted.append(backup)
        result.freed += backup.size
        log.info("Deleted backup %s (%s)", backup.name, format_size(backup.size))

        if backup.checksum:  # ...then its checksum file, so a crash leaves only a harmless orphan
            try:
                storage.delete(backup.checksum)
                result.freed += backup.checksum_size
                log.info("Deleted checksum file %s", backup.checksum)
            except StorageError as exc:
                log.warning(
                    "The backup was deleted but its checksum file %s could not be: %s",
                    backup.checksum, exc,
                )
    return result


def stored_backups(storage: StorageAdapter) -> list[tuple[BackupInfo, datetime]]:
    """Backups in storage, newest first. Only names this tool generated are included;
    the timestamp in the name is the real backup time."""
    choices = []
    for info in storage.list():
        parsed = parse_backup_filename(info.name)
        if parsed is not None:
            choices.append((info, parsed[1]))
    choices.sort(key=lambda c: (c[1], c[0].name), reverse=True)
    return choices
