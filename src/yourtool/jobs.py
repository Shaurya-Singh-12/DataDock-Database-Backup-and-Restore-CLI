"""Running one scheduled job. Each kind of job reuses the same code as the
matching command, so a scheduled backup behaves exactly like a manual one."""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from yourtool.backup import run_backup
from yourtool.databases import DatabaseError
from yourtool.integrity import verify_stored_backup
from yourtool.retention import Policy, build_plan, execute_prune, stored_backups
from yourtool.schedule import Job
from yourtool.storage import StorageError, get_storage
from yourtool.utils.formatting import format_size

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobOutcome:
    ok: bool
    message: str


def _backup(job: Job) -> JobOutcome:
    result = run_backup(job.config)
    detail = f"stored {result.location} ({format_size(result.size)})"
    if result.compression != "none" and result.original_size:
        detail += f", {result.compression} from {format_size(result.original_size)}"
    return JobOutcome(True, detail)


def _prune(job: Job) -> JobOutcome:
    # The config loader already insists on this; the check is repeated here because
    # this is the one job that deletes things.
    if not job.delete_without_asking:
        return JobOutcome(False, "refused: this prune job does not say delete_without_asking: true")
    config = job.config
    policy = Policy(config.keep_last, config.keep_daily, config.keep_weekly, config.keep_monthly)
    storage = get_storage(config)
    plan = build_plan(storage.list(), database=config.db_name, policy=policy)
    if not plan.decisions:
        return JobOutcome(True, f"no backups of '{config.db_name}' found; nothing to do")
    doomed = plan.to_delete()
    if not doomed:
        return JobOutcome(True, f"nothing to delete: all {plan.total_count()} backup(s) are kept")
    result = execute_prune(storage, doomed)
    summary = (
        f"deleted {len(result.deleted)} backup(s), freed {format_size(result.freed)}; "
        f"kept {plan.kept_count()}"
    )
    if result.failed:
        names = ", ".join(b.name for b, _ in result.failed)
        return JobOutcome(False, f"{summary}; could not delete: {names}")
    return JobOutcome(True, summary)


def _verify(job: Job) -> JobOutcome:
    storage = get_storage(job.config)
    backups = stored_backups(storage)
    if not backups:
        return JobOutcome(False, "no backups found to verify")
    chosen = backups if job.scope == "all" else backups[:1]
    problems, unverified, ok = [], 0, 0
    for info, _ in chosen:
        with tempfile.TemporaryDirectory(prefix="yourtool-") as tmp:
            try:
                result = verify_stored_backup(storage, info.name, Path(tmp))
            except StorageError as exc:  # includes IntegrityError
                problems.append(f"{info.name}: {exc}")
                continue
        if result.status == "ok":
            ok += 1
        else:
            unverified += 1
    summary = f"checked {len(chosen)} backup(s): {ok} ok, {unverified} without checksum, {len(problems)} failed"
    if problems:
        return JobOutcome(False, summary + " -- " + " | ".join(problems))
    return JobOutcome(True, summary)


_HANDLERS = {"backup": _backup, "prune": _prune, "verify": _verify}


def execute_job(job: Job) -> JobOutcome:
    """Run a job and report how it went. Never raises (except Ctrl+C)."""
    try:
        return _HANDLERS[job.command](job)
    except (DatabaseError, StorageError) as exc:
        return JobOutcome(False, str(exc))
    except Exception as exc:  # a bug must not stop the scheduler
        log.exception("Job '%s' crashed", job.name)
        return JobOutcome(False, f"unexpected error ({type(exc).__name__}): {exc}")
