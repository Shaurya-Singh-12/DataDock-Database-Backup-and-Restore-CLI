"""What each kind of scheduled job does when it runs."""

import re

import pytest
from fakes import FakeAdapter, FakeStorage, with_checksums

import yourtool.jobs as jobs_module
from yourtool.config import Config
from yourtool.cron import CronExpression
from yourtool.jobs import execute_job
from yourtool.schedule import Job


def make_job(command="backup", delete_without_asking=None, scope="latest", **settings):
    config = Config(db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="d", **settings)
    if delete_without_asking is None:
        delete_without_asking = command == "prune"
    return Job(name="the-job", command=command, cron=CronExpression.parse("* * * * *"), config=config,
               delete_without_asking=delete_without_asking, scope=scope)


def backup_name(day, db="d", ext=".dump"):
    return f"{db}_202610{day:02d}_100000{ext}"


@pytest.fixture
def stored(monkeypatch):
    """Six backups of 'd', three of 'other', and a file that is not ours."""
    files = {backup_name(day): b"x" * 1000 for day in range(10, 16)}
    files.update({backup_name(day, "other"): b"y" * 500 for day in (1, 2, 3)})
    files = with_checksums(files)
    files["notes.txt"] = b"mine"
    storage = FakeStorage(files=files)
    monkeypatch.setattr(jobs_module, "get_storage", lambda config: storage)
    return storage


# --------------------------------------------------------------- backup


@pytest.fixture
def backup_world(monkeypatch):
    adapter, storage = FakeAdapter(content=b"INSERT INTO t VALUES (1);\n" * 400), FakeStorage()
    for name in ("get_adapter", "get_storage"):
        monkeypatch.setattr(f"yourtool.backup.{name}", (lambda c, a=adapter, s=storage, n=name: a if n == "get_adapter" else s))
    return adapter, storage


def test_a_backup_job_makes_a_backup_and_its_checksum(backup_world):
    _, storage = backup_world
    outcome = execute_job(make_job("backup"))
    assert outcome.ok and outcome.message.startswith("stored /fake/d_")
    assert len(storage.files) == 2 and any(n.endswith(".sha256") for n in storage.files)


def test_a_backup_job_uses_its_own_compression_setting(backup_world):
    _, storage = backup_world
    outcome = execute_job(make_job("backup", compression="gzip"))
    assert outcome.ok and re.search(r"gzip from [\d.]+ KB", outcome.message)
    assert any(n.endswith(".dump.gz") for n in storage.files)


def test_a_failed_backup_is_reported_not_raised(backup_world):
    adapter, storage = backup_world
    adapter.fail = True
    outcome = execute_job(make_job("backup"))
    assert not outcome.ok and "database exploded" in outcome.message and storage.files == {}


def test_a_storage_failure_during_backup_is_reported(backup_world):
    _, storage = backup_world
    storage.fail = True
    outcome = execute_job(make_job("backup"))
    assert not outcome.ok and "disk full" in outcome.message


# ---------------------------------------------------------------- prune


def left(storage, db="d"):
    return sorted(n for n in storage.files if n.startswith(f"{db}_") and not n.endswith(".sha256"))


def test_a_prune_job_deletes_old_backups_and_their_checksums(stored):
    outcome = execute_job(make_job("prune", keep_last=2))
    assert outcome.ok and re.search(r"deleted 4 backup\(s\), freed [\d.]+ KB; kept 2", outcome.message)
    assert left(stored) == [backup_name(14), backup_name(15)]
    assert backup_name(10) + ".sha256" not in stored.files and backup_name(15) + ".sha256" in stored.files


def test_a_prune_job_never_touches_other_databases_or_foreign_files(stored):
    execute_job(make_job("prune", keep_last=1))
    assert len(left(stored, "other")) == 3 and stored.files["notes.txt"] == b"mine"


def test_a_prune_job_always_keeps_the_newest_backup(stored):
    execute_job(make_job("prune", keep_monthly=1))
    assert left(stored) == [backup_name(15)]


def test_a_prune_job_with_nothing_to_delete(stored):
    outcome = execute_job(make_job("prune", keep_last=50))
    assert outcome.ok and "nothing to delete: all 6 backup(s) are kept" in outcome.message
    assert stored.deleted == []


def test_a_prune_job_for_a_database_with_no_backups(stored):
    job = make_job("prune", keep_last=2)
    job = Job(**{**job.__dict__, "config": Config(db_type="postgres", db_host="h", db_port=5432, db_name="zzz", keep_last=2)})
    outcome = execute_job(job)
    assert outcome.ok and "no backups of 'zzz' found" in outcome.message and stored.deleted == []


def test_a_prune_job_without_the_safety_switch_refuses_even_if_it_somehow_got_this_far(stored):
    outcome = execute_job(make_job("prune", delete_without_asking=False, keep_last=1))
    assert not outcome.ok and "delete_without_asking" in outcome.message
    assert stored.deleted == []  # nothing was removed


def test_a_prune_job_that_cannot_delete_everything_says_which(stored):
    stored.fail_delete.add(backup_name(12))
    outcome = execute_job(make_job("prune", keep_last=2))
    assert not outcome.ok and backup_name(12) in outcome.message and "deleted 3 backup(s)" in outcome.message
    assert backup_name(12) in stored.files


# --------------------------------------------------------------- verify


def test_a_verify_job_checks_the_newest_backup_by_default(stored):
    stored.files[backup_name(10)] = b"damaged"  # an OLD backup is bad, but only the newest is checked
    outcome = execute_job(make_job("verify"))
    assert outcome.ok and "checked 1 backup(s): 1 ok, 0 without checksum, 0 failed" in outcome.message


def test_a_verify_job_fails_when_the_newest_backup_is_damaged(stored):
    stored.files[backup_name(15)] = b"damaged"
    outcome = execute_job(make_job("verify"))
    assert not outcome.ok and backup_name(15) in outcome.message
    assert "does not match its stored checksum" in outcome.message


def test_a_verify_job_with_scope_all_checks_everything_and_names_each_failure(stored):
    stored.files[backup_name(10)] = b"damaged"
    stored.files[backup_name(12)] = b"also damaged"
    outcome = execute_job(make_job("verify", scope="all"))
    assert not outcome.ok and "checked 9 backup(s): 7 ok, 0 without checksum, 2 failed" in outcome.message
    assert backup_name(10) in outcome.message and backup_name(12) in outcome.message


def test_a_backup_without_a_checksum_is_counted_but_is_not_a_failure(stored):
    del stored.files[backup_name(15) + ".sha256"]
    outcome = execute_job(make_job("verify"))
    assert outcome.ok and "1 without checksum" in outcome.message


def test_a_verify_job_with_no_backups_fails_because_that_is_a_problem(monkeypatch):
    monkeypatch.setattr(jobs_module, "get_storage", lambda config: FakeStorage())
    outcome = execute_job(make_job("verify"))
    assert not outcome.ok and outcome.message == "no backups found to verify"


# ------------------------------------------------------------ robustness


def test_an_unexpected_error_becomes_a_failed_outcome_and_is_logged(monkeypatch, caplog):
    def explode(config):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(jobs_module, "get_storage", explode)
    outcome = execute_job(make_job("verify"))
    assert not outcome.ok and "unexpected error (RuntimeError): something nobody planned for" in outcome.message
    assert "Traceback" in caplog.text


def test_ctrl_c_is_not_swallowed(monkeypatch):
    def interrupt(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(jobs_module, "get_storage", interrupt)
    with pytest.raises(KeyboardInterrupt):
        execute_job(make_job("verify"))
