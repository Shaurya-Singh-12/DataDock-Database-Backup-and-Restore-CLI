"""Small helpers for integration tests that talk to a REAL MySQL/MariaDB server."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def sql_string(value: str) -> str:
    """A value as a SQL string literal (for building test users only)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def client_path() -> str | None:
    return shutil.which("mysql") or shutil.which("mariadb")


@dataclass(frozen=True)
class MysqlServer:
    host: str = "localhost"
    port: str = "3306"
    user: str = "root"
    password: str = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> "MysqlServer":
        return cls(
            host=env.get("YOURTOOL_MYSQL_HOST") or "localhost",
            port=env.get("YOURTOOL_MYSQL_PORT") or "3306",
            user=env.get("YOURTOOL_MYSQL_USER") or "root",
            password=env.get("YOURTOOL_MYSQL_PASSWORD") or "",
        )

    def sql(self, statement: str, db: str | None = None, *, as_user: tuple[str, str] | None = None) -> str:
        """Run SQL and return the output (one line per row, tab separated)."""
        user, password = as_user or (self.user, self.password)
        env = dict(os.environ)
        env.pop("MYSQL_PWD", None)
        if password:
            env["MYSQL_PWD"] = password
        command = [
            client_path(), f"--host={self.host}", f"--port={self.port}", f"--user={user}",
            "--protocol=tcp", "--batch", "--skip-column-names", "--default-character-set=utf8mb4",
        ]
        if db:
            command.append(f"--database={db}")
        command.append(f"--execute={statement}")
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            raise AssertionError(f"mysql failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def database_exists(self, name: str) -> bool:
        literal = sql_string(name)
        return self.sql(f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = {literal}") == "1"

    def create_database(self, name: str) -> None:
        self.sql(f"CREATE DATABASE {quote_identifier(name)}")

    def drop_database(self, name: str) -> None:
        self.sql(f"DROP DATABASE IF EXISTS {quote_identifier(name)}")

    def count(self, db: str, table: str = "students") -> int:
        return int(self.sql(f"SELECT COUNT(*) FROM {quote_identifier(table)}", db))
