import pytest
from fakes import FakeAdapter, FakeStorage

from yourtool.config import Config
from yourtool.databases import DatabaseError
from yourtool.restore import RestoreCancelled, run_restore
from yourtool.storage import StorageError

NAME = "mydb_20261009_143022.dump"


def make_config(target="target_db") -> Config:
    return Config(
        db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name=target,
        storage_type="local", storage_path="./backups",
    )


def storage():
    return FakeStorage(files={NAME: b"dump-bytes"})


def always(plan):
    return True


def test_restore_into_existing_database():
    adapter = FakeAdapter(exists=True)
    result = run_restore(make_config(), NAME, confirm=always, adapter=adapter, storage=storage())
    assert adapter.calls == ["verify", "exists", ("restore", False)]
    assert adapter.restored_bytes == b"dump-bytes"
    assert result.target_db == "target_db"
    assert result.created_database is False


def test_restore_into_new_database_creates_it_first():
    adapter = FakeAdapter(exists=False)
    result = run_restore(make_config(), NAME, confirm=always, adapter=adapter, storage=storage())
    assert adapter.calls == ["verify", "exists", "create", ("restore", False)]
    assert result.created_database is True


def test_clean_is_passed_for_existing_database():
    adapter = FakeAdapter(exists=True)
    run_restore(make_config(), NAME, clean=True, confirm=always, adapter=adapter, storage=storage())
    assert ("restore", True) in adapter.calls


def test_clean_is_ignored_for_new_database():
    adapter = FakeAdapter(exists=False)
    run_restore(make_config(), NAME, clean=True, confirm=always, adapter=adapter, storage=storage())
    assert ("restore", False) in adapter.calls


def test_confirm_receives_accurate_plan():
    seen = []

    def confirm(plan):
        seen.append(plan)
        return True

    run_restore(
        make_config(), NAME, clean=True, confirm=confirm,
        adapter=FakeAdapter(exists=True), storage=storage(),
    )
    (plan,) = seen
    assert plan.backup_name == NAME
    assert plan.target_db == "target_db"
    assert plan.host == "h" and plan.port == 5432
    assert plan.target_exists is True and plan.clean is True


def test_declined_confirmation_changes_nothing():
    adapter = FakeAdapter(exists=False)
    with pytest.raises(RestoreCancelled, match="Nothing was changed"):
        run_restore(
            make_config(), NAME, confirm=lambda plan: False, adapter=adapter, storage=storage()
        )
    assert "create" not in adapter.calls
    assert not any(isinstance(c, tuple) for c in adapter.calls)  # no restore call


def test_corrupt_backup_is_caught_before_anything_changes():
    adapter = FakeAdapter(exists=False, verify_fails=True)
    asked = []
    with pytest.raises(DatabaseError, match="not a valid backup"):
        run_restore(
            make_config(), NAME, confirm=lambda p: asked.append(p) or True,
            adapter=adapter, storage=storage(),
        )
    assert adapter.calls == ["verify"]  # never even checked the database
    assert asked == []


def test_missing_backup():
    with pytest.raises(StorageError, match="not found"):
        run_restore(
            make_config(), "nope.dump", confirm=always, adapter=FakeAdapter(), storage=storage()
        )


def test_restore_failure_propagates():
    with pytest.raises(DatabaseError, match="restore exploded"):
        run_restore(
            make_config(), NAME, confirm=always,
            adapter=FakeAdapter(restore_fails=True), storage=storage(),
        )


def test_temp_copy_is_cleaned_up():
    adapter = FakeAdapter()
    run_restore(make_config(), NAME, confirm=always, adapter=adapter, storage=storage())
    assert not adapter.local_file.exists()
    assert not adapter.local_file.parent.exists()


def test_confirm_is_required():
    with pytest.raises(TypeError):
        run_restore(make_config(), NAME, adapter=FakeAdapter(), storage=storage())
