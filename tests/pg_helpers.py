"""Small helpers for integration tests that talk to a REAL PostgreSQL server."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping


def quote_identifier(name: str) -> str:
    """Quote a database/table name for use in SQL ("my db" -> "\"my db\"")."""
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class PgServer:
    host: str = "localhost"
    port: str = "5432"
    user: str = "postgres"
    password: str = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "PgServer":
        """Same variable names the tool itself uses, so setup is the same."""
        return cls(
            host=env.get("YOURTOOL_DB_HOST") or "localhost",
            port=env.get("YOURTOOL_DB_PORT") or "5432",
            user=env.get("YOURTOOL_DB_USER") or "postgres",
            password=env.get("YOURTOOL_DB_PASSWORD") or "",
        )

    def psql(self, sql: str, db: str = "postgres") -> str:
        """Run one SQL string and return its output. Raises with psql's message."""
        env = dict(os.environ)
        if self.password:
            env["PGPASSWORD"] = self.password
        result = subprocess.run(
            [
                shutil.which("psql"), "-h", self.host, "-p", self.port, "-U", self.user,
                "-d", db, "--no-psqlrc", "-tA", "-v", "ON_ERROR_STOP=1", "--no-password",
                "-c", sql,
            ],
            env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError(f"psql failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def database_exists(self, name: str) -> bool:
        literal = name.replace("'", "''")
        return self.psql(f"SELECT 1 FROM pg_database WHERE datname = '{literal}';") == "1"

    def create_database(self, name: str) -> None:
        self.psql(f"CREATE DATABASE {quote_identifier(name)};")

    def drop_database(self, name: str) -> None:
        # WITH (FORCE) also disconnects anyone still connected (PostgreSQL 13+).
        self.psql(f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE);")

    def count(self, db: str, table: str = "students") -> int:
        return int(self.psql(f"SELECT count(*) FROM {quote_identifier(table)};", db))
