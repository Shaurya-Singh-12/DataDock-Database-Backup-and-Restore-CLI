from datetime import datetime, timedelta, timezone

import pytest

from yourtool.utils.naming import backup_filename, parse_backup_filename

NOW = datetime(2026, 10, 9, 14, 30, 22, tzinfo=timezone.utc)


def test_basic_name():
    assert backup_filename("testdb", ".dump", NOW) == "testdb_20261009_143022.dump"


def test_extension_without_dot():
    assert backup_filename("testdb", "dump", NOW) == "testdb_20261009_143022.dump"


def test_non_utc_time_is_converted_to_utc():
    india = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 10, 9, 20, 0, 22, tzinfo=india)  # 14:30:22 UTC
    assert backup_filename("testdb", ".dump", local) == "testdb_20261009_143022.dump"


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        backup_filename("testdb", ".dump", datetime(2026, 10, 9, 14, 30, 22))


def test_unsafe_characters_replaced():
    name = backup_filename("my db/../evil", ".dump", NOW)
    assert "/" not in name and " " not in name
    assert name.endswith("_20261009_143022.dump")


def test_empty_db_name_falls_back():
    assert backup_filename("///", ".dump", NOW).startswith("database_")


def test_default_clock_is_utc_and_parseable():
    name = backup_filename("testdb", ".dump")
    db, when = parse_backup_filename(name)
    assert db == "testdb"
    assert when.tzinfo is timezone.utc
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 5


def test_round_trip():
    name = backup_filename("testdb", ".dump", NOW)
    assert parse_backup_filename(name) == ("testdb", NOW)


def test_names_sort_chronologically():
    earlier = backup_filename("db", ".dump", NOW)
    later = backup_filename("db", ".dump", NOW + timedelta(seconds=1))
    assert earlier < later


@pytest.mark.parametrize("bad", ["notes.txt", "db_2026.dump", "db_20261399_999999.dump"])
def test_parse_rejects_foreign_names(bad):
    assert parse_backup_filename(bad) is None


def test_underscores_in_database_names_are_kept():
    assert backup_filename("college_grades", ".dump", NOW) == "college_grades_20261009_143022.dump"


def test_underscore_names_round_trip():
    name = backup_filename("my_db_v2", ".dump", NOW)
    assert parse_backup_filename(name) == ("my_db_v2", NOW)


def test_names_that_differ_only_by_underscore_or_hyphen_stay_distinct():
    a = backup_filename("a_b", ".dump", NOW)
    b = backup_filename("a-b", ".dump", NOW)
    assert a != b


def test_database_name_that_looks_like_a_timestamp_still_parses():
    name = backup_filename("x_20200101_000000", ".dump", NOW)
    assert parse_backup_filename(name) == ("x_20200101_000000", NOW)


# ---- checksum files and compressed backups (Step 11) ----

from yourtool.utils.naming import checksum_name, is_checksum_name  # noqa: E402


def test_checksum_name():
    assert checksum_name("db_20261010_100000.dump.gz") == "db_20261010_100000.dump.gz.sha256"
    assert is_checksum_name("db_20261010_100000.dump.sha256")
    assert not is_checksum_name("db_20261010_100000.dump")


def test_a_checksum_file_is_not_a_backup():
    assert parse_backup_filename("db_20261010_100000.dump.sha256") is None
    assert parse_backup_filename("db_20261010_100000.dump.gz.sha256") is None


def test_compressed_backup_names_parse():
    name = backup_filename("college_grades", ".sql.gz", NOW)
    assert name == "college_grades_20261009_143022.sql.gz"
    assert parse_backup_filename(name) == ("college_grades", NOW)
