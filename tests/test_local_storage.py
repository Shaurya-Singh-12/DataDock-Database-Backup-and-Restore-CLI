import pytest

from yourtool.config import Config
from yourtool.storage import LocalStorage, StorageError, get_storage


@pytest.fixture
def source(tmp_path):
    f = tmp_path / "source.dump"
    f.write_bytes(b"backup-bytes")
    return f


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "backups")


def test_upload_creates_folder_and_copies(storage, source):
    location = storage.upload(source, "a_20261009_100000.dump")
    assert (storage.directory / "a_20261009_100000.dump").read_bytes() == b"backup-bytes"
    assert location.endswith("a_20261009_100000.dump")


def test_upload_leaves_no_partial_files(storage, source):
    storage.upload(source, "a_20261009_100000.dump")
    assert [p.name for p in storage.directory.iterdir()] == ["a_20261009_100000.dump"]


def test_upload_refuses_to_overwrite(storage, source):
    storage.upload(source, "x.dump")
    with pytest.raises(StorageError, match="already exists"):
        storage.upload(source, "x.dump")


def test_upload_missing_source(storage, tmp_path):
    with pytest.raises(StorageError, match="does not exist"):
        storage.upload(tmp_path / "nope", "x.dump")


def test_list_empty_when_folder_missing(storage):
    assert storage.list() == []


def test_list_newest_first_and_ignores_partials(storage, source):
    import os

    storage.upload(source, "old.dump")
    storage.upload(source, "new.dump")
    os.utime(storage.directory / "old.dump", (1_000_000, 1_000_000))
    (storage.directory / "half.dump.part").write_bytes(b"x")

    names = [b.name for b in storage.list()]
    assert names == ["new.dump", "old.dump"]


def test_list_reports_size_and_utc_time(storage, source):
    storage.upload(source, "x.dump")
    (info,) = storage.list()
    assert info.size == len(b"backup-bytes")
    assert info.modified.utcoffset().total_seconds() == 0


def test_download_round_trip(storage, source, tmp_path):
    storage.upload(source, "x.dump")
    out = storage.download("x.dump", tmp_path / "sub" / "copy.dump")
    assert out.read_bytes() == b"backup-bytes"


def test_download_missing(storage, tmp_path):
    with pytest.raises(StorageError, match="not found"):
        storage.download("nope.dump", tmp_path / "x")


def test_delete(storage, source):
    storage.upload(source, "x.dump")
    storage.delete("x.dump")
    assert storage.list() == []


def test_delete_missing(storage):
    with pytest.raises(StorageError, match="not found"):
        storage.delete("nope.dump")


@pytest.mark.parametrize(
    "bad", ["../escape.dump", "a/b.dump", "a\\b.dump", "..", ".", "", "x.dump.part"]
)
def test_unsafe_names_rejected(storage, source, bad):
    with pytest.raises(StorageError, match="Invalid backup name"):
        storage.upload(source, bad)
    with pytest.raises(StorageError, match="Invalid backup name"):
        storage.delete(bad)


def test_get_storage_local(tmp_path):
    cfg = Config(
        db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="d",
        storage_type="local", storage_path=str(tmp_path / "b"),
    )
    assert isinstance(get_storage(cfg), LocalStorage)


def test_get_storage_unknown(tmp_path):
    cfg = Config(
        db_type="postgres", db_host="h", db_port=5432, db_user="u", db_name="d",
        storage_type="ftp", storage_path="x",
    )
    with pytest.raises(StorageError, match="No adapter"):
        get_storage(cfg)


def test_exists(storage, source):
    assert storage.exists("a.dump") is False
    storage.upload(source, "a.dump")
    assert storage.exists("a.dump") is True


def test_exists_is_false_when_the_folder_is_missing(tmp_path):
    from yourtool.storage import LocalStorage

    assert LocalStorage(tmp_path / "nope").exists("a.dump") is False


@pytest.mark.parametrize("bad", ["../x", "a/b", "", ".."])
def test_exists_rejects_unsafe_names(storage, bad):
    with pytest.raises(StorageError, match="Invalid backup name"):
        storage.exists(bad)
