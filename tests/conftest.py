"""Shared test setup.

Three jobs:

1. Every test starts with a clean environment: no YOURTOOL_* or PGPASSWORD
   variables from the machine running the tests can leak in and change the
   outcome (this is why tests behave the same on your laptop and in CI).
2. Tests marked ``integration`` only run when you opt in with
   YOURTOOL_INTEGRATION=1 *and* the PostgreSQL tools are installed.
3. Fixtures that give integration tests a throwaway database to work on, so
   they never touch (or depend on) your own databases.
"""

import os
import shutil
import uuid

import pytest
from my_helpers import MysqlServer, client_path
from pg_helpers import PgServer

from yourtool.utils.logsetup import reset_logging

# Read the real connection settings NOW, before the per-test cleanup below
# removes them from the environment.
_INTEGRATION_ENABLED = os.environ.get("YOURTOOL_INTEGRATION") == "1"
_SERVER = PgServer.from_environment(os.environ)
_REQUIRED_TOOLS = ("pg_dump", "pg_restore", "psql")

_MYSQL_ENABLED = os.environ.get("YOURTOOL_INTEGRATION_MYSQL") == "1"
_MYSQL = MysqlServer.from_environment(os.environ)


def _mysql_tools_found() -> bool:
    return bool(client_path()) and bool(shutil.which("mysqldump") or shutil.which("mariadb-dump"))


def pytest_collection_modifyitems(config, items):
    """Skip real-database tests unless the person opted in and the tools exist."""
    reasons = {}

    if not _INTEGRATION_ENABLED:
        reasons["integration"] = (
            "integration test: set YOURTOOL_INTEGRATION=1 to run against a real PostgreSQL"
        )
    else:
        missing = [t for t in _REQUIRED_TOOLS if shutil.which(t) is None]
        if missing:
            reasons["integration"] = (
                f"integration test: PostgreSQL tools not found on PATH: {', '.join(missing)}"
            )

    if not _MYSQL_ENABLED:
        reasons["mysql_integration"] = (
            "integration test: set YOURTOOL_INTEGRATION_MYSQL=1 to run against a real MySQL/MariaDB"
        )
    elif not _mysql_tools_found():
        reasons["mysql_integration"] = (
            "integration test: MySQL/MariaDB client tools (mysql, mysqldump) not found on PATH"
        )

    for item in items:
        for marker, reason in reasons.items():
            if marker in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("YOURTOOL_", "AWS_")) or name in {"PGPASSWORD", "MYSQL_PWD"}:
            monkeypatch.delenv(name, raising=False)
    # Safety: no test can ever pick up your real AWS credentials, profiles, or
    # the cloud metadata service. Tests that need S3 use a local mock server.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


@pytest.fixture(autouse=True)
def _clean_logging():
    """Every test starts and ends with no leftover log handlers or secrets."""
    reset_logging()
    yield
    reset_logging()


# ------------------------------------------------------- real PostgreSQL


@pytest.fixture(scope="session")
def _server_reachable():
    """Fail once, with one clear message, if the server can't be used."""
    problem = None
    try:
        _SERVER.psql("SELECT 1;")
    except AssertionError as exc:
        problem = str(exc).splitlines()[0]  # psql repeats its message; one line is enough
    if problem:  # raised outside the except block so the message isn't printed twice
        pytest.fail(
            f"Cannot use PostgreSQL at {_SERVER.host}:{_SERVER.port} as user "
            f"'{_SERVER.user}'.\n{problem}\n\n"
            "Set YOURTOOL_DB_PASSWORD (and YOURTOOL_DB_HOST / YOURTOOL_DB_PORT / "
            "YOURTOOL_DB_USER if they are not localhost / 5432 / postgres), or "
            "unset YOURTOOL_INTEGRATION to skip these tests.",
            pytrace=False,
        )


@pytest.fixture
def pg(_server_reachable, monkeypatch) -> PgServer:
    """The server to test against. Also exports its settings as environment
    variables so the command line under test picks them up, exactly as a
    person (or CI) would configure it."""
    monkeypatch.setenv("YOURTOOL_DB_HOST", _SERVER.host)
    monkeypatch.setenv("YOURTOOL_DB_PORT", _SERVER.port)
    monkeypatch.setenv("YOURTOOL_DB_USER", _SERVER.user)
    if _SERVER.password:
        monkeypatch.setenv("YOURTOOL_DB_PASSWORD", _SERVER.password)
    return _SERVER


@pytest.fixture
def make_database(pg):
    """Create empty, uniquely named databases; all are dropped afterwards."""
    created = []

    def make(prefix="yt_test", *, create=True, name=None):
        name = name or f"{prefix}_{uuid.uuid4().hex[:8]}"
        created.append(name)  # registered first, so it is cleaned up even if creation fails
        if create:
            pg.create_database(name)
        return name

    yield make
    for name in created:
        pg.drop_database(name)


@pytest.fixture
def source_db(pg, make_database):
    """A throwaway database holding a 3-row `students` table."""
    name = make_database("yt_src")
    pg.psql(
        "CREATE TABLE students (id SERIAL PRIMARY KEY, name TEXT, grade INT);"
        "INSERT INTO students (name, grade) VALUES ('Alice', 90), ('Bob', 85), ('Cara', 78);",
        name,
    )
    return name


# ---------------------------------------------------------- real MySQL/MariaDB


@pytest.fixture(scope="session")
def _mysql_reachable():
    """Fail once, with one clear message, if the MySQL server can't be used."""
    problem = None
    try:
        _MYSQL.sql("SELECT 1")
    except AssertionError as exc:
        problem = str(exc).splitlines()[0]
    if problem:
        pytest.fail(
            f"Cannot use MySQL/MariaDB at {_MYSQL.host}:{_MYSQL.port} as user "
            f"'{_MYSQL.user}'.\n{problem}\n\n"
            "Set YOURTOOL_MYSQL_PASSWORD (and YOURTOOL_MYSQL_HOST / YOURTOOL_MYSQL_PORT / "
            "YOURTOOL_MYSQL_USER if they are not localhost / 3306 / root), or unset "
            "YOURTOOL_INTEGRATION_MYSQL to skip these tests.",
            pytrace=False,
        )


@pytest.fixture
def my(_mysql_reachable, monkeypatch) -> MysqlServer:
    """The MySQL server to test against. Its settings are also exported as
    environment variables so the command line under test picks them up."""
    monkeypatch.setenv("YOURTOOL_DB_TYPE", "mysql")
    monkeypatch.setenv("YOURTOOL_DB_HOST", _MYSQL.host)
    monkeypatch.setenv("YOURTOOL_DB_PORT", _MYSQL.port)
    monkeypatch.setenv("YOURTOOL_DB_USER", _MYSQL.user)
    if _MYSQL.password:
        monkeypatch.setenv("YOURTOOL_DB_PASSWORD", _MYSQL.password)
    return _MYSQL


@pytest.fixture
def make_my_database(my):
    """Create uniquely named databases; all are dropped afterwards."""
    created = []

    def make(prefix="yt_test", *, create=True, name=None):
        name = name or f"{prefix}_{uuid.uuid4().hex[:8]}"
        created.append(name)
        if create:
            my.create_database(name)
        return name

    yield make
    for name in created:
        my.drop_database(name)


@pytest.fixture
def my_source_db(my, make_my_database):
    """A throwaway database: a table (with a non-ASCII value), a view and a procedure."""
    name = make_my_database("yt_src")
    my.sql(
        "CREATE TABLE students (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL, "
        "grade INT NOT NULL) ENGINE=InnoDB;"
        "INSERT INTO students (name, grade) VALUES ('Alice', 90), ('Bob', 85), ('Cara', 78), ('Zo\u00eb', 99);"
        "CREATE VIEW top_students AS SELECT name FROM students WHERE grade >= 85;"
        "CREATE PROCEDURE count_students() SELECT COUNT(*) FROM students;",
        name,
    )
    return name


# ------------------------------------------------------------ S3 (mock server)


@pytest.fixture(scope="session")
def moto_server():
    """A real HTTP server that speaks the S3 protocol (moto), on a free port.
    Our code talks to it exactly as it would talk to any S3-compatible service."""
    pytest.importorskip("moto", reason="install the dev extras: pip install -e '.[dev]'")
    from moto.server import ThreadedMotoServer

    # 127.0.0.1 on purpose, never moto's default of 0.0.0.0: that address only works
    # as a *destination* on some systems (it fails on Windows), and it would also make
    # the mock server reachable from the whole network while the tests run.
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    _, port = server.get_host_and_port()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def s3_env(moto_server, monkeypatch):
    """Dummy credentials (the mock server accepts any) and the server's address."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    return moto_server


@pytest.fixture
def s3_client(s3_env):
    import boto3

    return boto3.client("s3", endpoint_url=s3_env, region_name="us-east-1")


@pytest.fixture
def s3_bucket(s3_client):
    """A fresh, empty bucket for one test; emptied and removed afterwards."""
    name = f"yt-test-{uuid.uuid4().hex[:12]}"
    s3_client.create_bucket(Bucket=name)
    yield name
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=name):
        for obj in page.get("Contents", []):
            s3_client.delete_object(Bucket=name, Key=obj["Key"])
    s3_client.delete_bucket(Bucket=name)
