"""The retention rules: which backups are kept, and the safety properties."""

import logging
import random
from datetime import datetime, timedelta, timezone

import pytest
from fakes import FakeStorage

from yourtool.retention import (
    Backup,
    Policy,
    build_plan,
    execute_prune,
    plan_retention,
)
from yourtool.storage.base import BackupInfo
from yourtool.utils.naming import checksum_name, parse_backup_filename

UTC = timezone.utc


def at(year, month, day, hour=10, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make(when, db="d", ext=".dump"):
    name = f"{db}_{when:%Y%m%d_%H%M%S}{ext}"
    return Backup(name=name, database=db, when=when, size=100)


def kept(decisions):
    return [d.backup.when for d in decisions if d.keep]


def deleted(decisions):
    return [d.backup.when for d in decisions if not d.keep]


# ------------------------------------------------------------ keep_last


def test_keep_last_keeps_the_newest_n():
    backups = [make(at(2026, 10, d)) for d in range(1, 11)]
    result = plan_retention(backups, Policy(keep_last=3))
    assert kept(result) == [at(2026, 10, 10), at(2026, 10, 9), at(2026, 10, 8)]
    assert len(deleted(result)) == 7


def test_keep_last_larger_than_the_number_of_backups_keeps_everything():
    backups = [make(at(2026, 10, d)) for d in (1, 2)]
    assert deleted(plan_retention(backups, Policy(keep_last=10))) == []


def test_result_is_newest_first_whatever_the_input_order():
    backups = [make(at(2026, 10, d)) for d in (5, 1, 9, 3)]
    result = plan_retention(backups, Policy(keep_last=1))
    assert [d.backup.when for d in result] == sorted((b.when for b in backups), reverse=True)


# ----------------------------------------------------------- keep_daily


def test_keep_daily_keeps_the_newest_backup_of_each_recent_day():
    backups = [
        make(at(2026, 10, 18, 14)), make(at(2026, 10, 18, 9)),   # two on the 18th
        make(at(2026, 10, 17, 10)),
        make(at(2026, 10, 15, 10)), make(at(2026, 10, 15, 8)),   # no backups on the 16th
        make(at(2026, 10, 10, 10)),
    ]
    result = plan_retention(backups, Policy(keep_daily=3))
    assert kept(result) == [at(2026, 10, 18, 14), at(2026, 10, 17, 10), at(2026, 10, 15, 10)]
    # "3 days" means 3 days THAT HAVE BACKUPS, so the gap on the 16th does not use one up
    assert deleted(result) == [at(2026, 10, 18, 9), at(2026, 10, 15, 8), at(2026, 10, 10, 10)]


# ---------------------------------------------------------- keep_weekly


def test_keep_weekly_keeps_the_newest_backup_of_each_recent_week():
    backups = [make(at(2026, 10, 18, 14)), make(at(2026, 10, 15)), make(at(2026, 10, 12)),   # week 42
               make(at(2026, 10, 10)), make(at(2026, 10, 6)),                                  # week 41
               make(at(2026, 9, 29))]                                                          # week 40
    result = plan_retention(backups, Policy(keep_weekly=2))
    assert kept(result) == [at(2026, 10, 18, 14), at(2026, 10, 10)]


def test_weeks_follow_iso_numbering_across_new_year():
    """31 Dec 2026 and 1 Jan 2027 are in the SAME ISO week (week 53 of 2026)."""
    backups = [make(at(2027, 1, 1)), make(at(2026, 12, 31)), make(at(2027, 1, 4))]  # 4 Jan: week 1
    result = plan_retention(backups, Policy(keep_weekly=2))
    assert kept(result) == [at(2027, 1, 4), at(2027, 1, 1)]
    assert deleted(result) == [at(2026, 12, 31)]


# --------------------------------------------------------- keep_monthly


def test_keep_monthly_keeps_the_newest_backup_of_each_recent_month():
    backups = [make(at(2026, 10, 18)), make(at(2026, 10, 2)), make(at(2026, 9, 30)),
               make(at(2026, 9, 1)), make(at(2026, 8, 15))]
    result = plan_retention(backups, Policy(keep_monthly=2))
    assert kept(result) == [at(2026, 10, 18), at(2026, 9, 30)]


# ------------------------------------------------- combining the rules


def test_a_backup_is_kept_if_any_rule_keeps_it_and_says_which():
    backups = [make(at(2026, 10, 18)), make(at(2026, 10, 17)), make(at(2026, 10, 1)),
               make(at(2026, 9, 5)), make(at(2026, 8, 20))]
    result = plan_retention(backups, Policy(keep_last=1, keep_daily=2, keep_monthly=3))
    reasons = {d.backup.when: d.reasons for d in result}
    assert reasons[at(2026, 10, 18)] == ("last", "daily", "monthly")
    assert reasons[at(2026, 10, 17)] == ("daily",)
    assert reasons[at(2026, 10, 1)] == ()                       # Oct's newest is the 18th
    assert reasons[at(2026, 9, 5)] == ("monthly",)
    assert reasons[at(2026, 8, 20)] == ("monthly",)


def test_no_backups_is_not_an_error():
    assert plan_retention([], Policy(keep_last=3)) == []


def test_a_policy_with_no_rules_is_refused():
    with pytest.raises(ValueError, match="at least one rule"):
        plan_retention([make(at(2026, 10, 1))], Policy())


def test_policy_description():
    assert Policy(keep_last=3, keep_monthly=6).describe() == "last 3, monthly 6"
    assert Policy().is_empty() and not Policy(keep_daily=1).is_empty()


# ------------------------------------------------ safety, checked at random


def random_history(rng):
    start = at(2025, 1, 1)
    count = rng.randint(1, 40)
    stamps = {start + timedelta(minutes=rng.randint(0, 60 * 24 * 700)) for _ in range(count)}
    return [make(t.replace(second=0)) for t in sorted(stamps)]


def random_policy(rng):
    while True:
        policy = Policy(*(rng.choice([None, rng.randint(1, 9)]) for _ in range(4)))
        if not policy.is_empty():
            return policy


def test_the_newest_backup_is_always_kept():
    """300 random backup histories and random rules: the newest backup must survive each."""
    for seed in range(300):
        rng = random.Random(seed)
        backups, policy = random_history(rng), random_policy(rng)
        result = plan_retention(backups, policy)
        assert result[0].keep, f"seed {seed}: the newest backup was marked for deletion by {policy}"
        assert result[0].backup.when == max(b.when for b in backups), f"seed {seed}"


def test_keeping_more_never_keeps_fewer():
    """Raising any rule's number can only add kept backups, never remove one."""
    for seed in range(150):
        rng = random.Random(1000 + seed)
        backups, policy = random_history(rng), random_policy(rng)
        before = {d.backup.name for d in plan_retention(backups, policy) if d.keep}
        bigger = Policy(*(None if v is None else v + rng.randint(0, 3) for v in
                          (policy.keep_last, policy.keep_daily, policy.keep_weekly, policy.keep_monthly)))
        after = {d.backup.name for d in plan_retention(backups, bigger) if d.keep}
        assert before <= after, f"seed {seed}: {policy} -> {bigger} lost {before - after}"


def test_keep_last_keeps_exactly_that_many():
    for seed in range(150):
        rng = random.Random(2000 + seed)
        backups, n = random_history(rng), random.Random(seed).randint(1, 12)
        result = plan_retention(backups, Policy(keep_last=n))
        assert sum(d.keep for d in result) == min(n, len(backups)), f"seed {seed}"


def test_daily_keeps_one_per_day_and_never_more_than_n_days():
    for seed in range(150):
        rng = random.Random(3000 + seed)
        backups, n = random_history(rng), random.Random(seed).randint(1, 10)
        result = plan_retention(backups, Policy(keep_daily=n))
        kept_backups = [d.backup for d in result if d.keep]
        days = [b.when.date() for b in kept_backups]
        assert len(days) == len(set(days)) <= n, f"seed {seed}: more than one per day, or more than {n} days"
        for b in kept_backups:  # and it is the newest of its day
            newest = max(o.when for o in backups if o.when.date() == b.when.date())
            assert b.when == newest, f"seed {seed}"


def test_the_same_input_always_gives_the_same_plan():
    rng = random.Random(7)
    backups, policy = random_history(rng), random_policy(rng)
    shuffled = backups[:]
    rng.shuffle(shuffled)
    assert plan_retention(backups, policy) == plan_retention(shuffled, policy)


# ------------------------------------------------------------- build_plan


def info(name, size=100):
    return BackupInfo(name=name, size=size, modified=at(2026, 1, 1))


def listing(*names):
    return [info(n) for n in names]


def test_only_the_chosen_database_is_considered():
    files = listing("a_20261001_100000.dump", "a_20261002_100000.dump",
                    "b_20261001_100000.dump", "b_20261002_100000.dump")
    plan = build_plan(files, database="a", policy=Policy(keep_last=1))
    assert list(plan.decisions) == ["a"]
    assert [b.name for b in plan.to_delete()] == ["a_20261001_100000.dump"]  # b is never in play


def test_a_database_name_that_is_a_prefix_of_another_is_not_confused_with_it():
    files = listing("shop_20261001_100000.dump", "shop_20261002_100000.dump",
                    "shop_v2_20261001_100000.dump", "shop_v2_20261002_100000.dump")
    plan = build_plan(files, database="shop", policy=Policy(keep_last=1))
    assert [b.name for b in plan.to_delete()] == ["shop_20261001_100000.dump"]


def test_the_database_name_is_matched_the_way_file_names_are_made():
    # "my db" appears in file names as "my-db"
    files = listing("my-db_20261001_100000.dump", "my-db_20261002_100000.dump")
    plan = build_plan(files, database="my db", policy=Policy(keep_last=1))
    assert [b.name for b in plan.to_delete()] == ["my-db_20261001_100000.dump"]


def test_all_databases_judges_each_separately():
    files = listing("a_20261001_100000.dump", "a_20261002_100000.dump", "a_20261003_100000.dump",
                    "b_20261001_100000.dump")
    plan = build_plan(files, database=None, policy=Policy(keep_last=2))
    assert sorted(plan.decisions) == ["a", "b"]
    assert [b.name for b in plan.to_delete()] == ["a_20261001_100000.dump"]  # b has only one


def test_files_that_are_not_ours_are_never_considered():
    files = listing("a_20261001_100000.dump", "a_20261002_100000.dump",
                    "notes.txt", "holiday.jpg", "a_not_a_timestamp.dump", "backup.sql")
    plan = build_plan(files, database=None, policy=Policy(keep_last=1))
    assert [b.name for b in plan.to_delete()] == ["a_20261001_100000.dump"]
    assert plan.total_count() == 2


def test_plain_and_compressed_backups_of_one_database_are_judged_together():
    files = listing("a_20261001_100000.dump", "a_20261002_100000.dump.gz", "a_20261003_100000.dump")
    plan = build_plan(files, database="a", policy=Policy(keep_last=2))
    assert [b.name for b in plan.to_delete()] == ["a_20261001_100000.dump"]


def test_a_backup_is_paired_with_its_checksum_file():
    name = "a_20261001_100000.dump.gz"
    files = [info(name, 1000), info(checksum_name(name), 90), info("a_20261002_100000.dump.gz", 1000)]
    plan = build_plan(files, database="a", policy=Policy(keep_last=1))
    (doomed,) = plan.to_delete()
    assert doomed.name == name and doomed.checksum == checksum_name(name) and doomed.checksum_size == 90


def test_checksum_files_are_never_counted_as_backups():
    files = listing("a_20261001_100000.dump", "a_20261001_100000.dump.sha256")
    plan = build_plan(files, database="a", policy=Policy(keep_last=5))
    assert plan.total_count() == 1


def test_orphan_checksum_files_are_reported_not_deleted():
    files = listing("a_20261002_100000.dump", "a_20261001_100000.dump.sha256",  # its backup is gone
                    "b_20260101_100000.dump.sha256")                              # another database's
    plan = build_plan(files, database="a", policy=Policy(keep_last=1))
    assert plan.orphan_checksums == ("a_20261001_100000.dump.sha256",)
    assert plan.to_delete() == []


def test_nothing_found_gives_an_empty_plan():
    plan = build_plan(listing("notes.txt"), database="a", policy=Policy(keep_last=1))
    assert plan.decisions == {} and plan.to_delete() == [] and plan.total_count() == 0


# ----------------------------------------------------------- execute_prune


def stored(*names):
    return FakeStorage(files={n: b"x" * 10 for n in names})


def doomed(name, checksum=True):
    parsed = parse_backup_filename(name)
    return Backup(name=name, database=parsed[0], when=parsed[1], size=10,
                  checksum=checksum_name(name) if checksum else None, checksum_size=5)


def test_the_backup_is_deleted_before_its_checksum_file():
    name = "a_20261001_100000.dump"
    storage = stored(name, checksum_name(name))
    result = execute_prune(storage, [doomed(name)])
    assert storage.deleted == [name, checksum_name(name)]  # never the other way round
    assert storage.files == {} and len(result.deleted) == 1 and result.freed == 15


def test_a_backup_without_a_checksum_file_is_fine():
    name = "a_20261001_100000.dump"
    storage = stored(name)
    result = execute_prune(storage, [doomed(name, checksum=False)])
    assert storage.deleted == [name] and result.freed == 10


def test_one_failure_does_not_stop_the_others_and_is_reported():
    names = ["a_20261001_100000.dump", "a_20261002_100000.dump", "a_20261003_100000.dump"]
    storage = stored(*names)
    storage.fail_delete.add(names[1])
    result = execute_prune(storage, [doomed(n, checksum=False) for n in names])
    assert [b.name for b in result.deleted] == [names[0], names[2]]
    assert [b.name for b, _ in result.failed] == [names[1]]
    assert "permission denied" in result.failed[0][1]
    assert names[1] in storage.files  # the one that failed is still there


def test_if_the_backup_cannot_be_deleted_its_checksum_file_is_kept():
    """Otherwise a backup that still exists would lose its integrity check."""
    name = "a_20261001_100000.dump"
    storage = stored(name, checksum_name(name))
    storage.fail_delete.add(name)
    execute_prune(storage, [doomed(name)])
    assert checksum_name(name) in storage.files


def test_a_checksum_file_that_cannot_be_deleted_is_a_warning_not_a_failure(caplog):
    caplog.set_level(logging.WARNING, logger="yourtool")
    name = "a_20261001_100000.dump"
    storage = stored(name, checksum_name(name))
    storage.fail_delete.add(checksum_name(name))
    result = execute_prune(storage, [doomed(name)])
    assert len(result.deleted) == 1 and result.failed == []
    assert "checksum file" in caplog.text


def test_deletions_are_logged_for_the_audit_trail(caplog):
    caplog.set_level(logging.INFO, logger="yourtool")
    name = "a_20261001_100000.dump"
    execute_prune(stored(name, checksum_name(name)), [doomed(name)])
    assert f"Deleted backup {name}" in caplog.text
