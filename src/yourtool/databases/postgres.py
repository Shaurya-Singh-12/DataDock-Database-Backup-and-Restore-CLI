"""PostgreSQL adapter. Uses the official PostgreSQL command-line tools
(pg_dump, pg_restore, psql)."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from yourtool.config import Config
from yourtool.databases.base import DatabaseAdapter, DatabaseError

log = logging.getLogger(__name__)


class PostgresAdapter(DatabaseAdapter):
    file_extension = ".dump"  # PostgreSQL "custom" format
    password_env_var = "PGPASSWORD"
    output_is_compressed = True  # the "custom" format is compressed by pg_dump

    # psql needs *some* database to connect to when checking for / creating
    # another one. "postgres" exists on every normal installation.
    _MAINTENANCE_DB = "postgres"

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------ helpers

    def _build_env(self) -> dict[str, str]:
        """Environment for the subprocess.

        The password goes in PGPASSWORD (an environment variable), never in
        the command line, where other users could see it in the process list.
        If no password was configured we leave the environment alone, so a
        PGPASSWORD you already set or a .pgpass file still works.
        """
        env = os.environ.copy()
        if self.config.db_password:
            env["PGPASSWORD"] = self.config.db_password
        return env

    def _connection_args(self, dbname: str | None = None) -> list[str]:
        c = self.config
        return [
            f"--host={c.db_host}",
            f"--port={c.db_port}",
            f"--username={c.db_user}",
            f"--dbname={dbname or c.db_name}",
            "--no-password",  # fail with an error instead of waiting for a prompt
        ]

    def _run(
        self, tool: str, args: list[str], input_text: str | None = None
    ) -> subprocess.CompletedProcess:
        executable = shutil.which(tool)
        if executable is None:
            raise DatabaseError(
                f"{tool} was not found. Install the PostgreSQL command-line tools "
                "and make sure their 'bin' folder is on your PATH."
            )
        # The password travels in the environment, never in the arguments,
        # so it is safe to log the full command.
        log.debug("Running: %s", shlex.join([executable, *args]))
        if input_text is not None:
            log.debug("SQL on stdin: %s", input_text)
        started = time.monotonic()
        try:
            result = subprocess.run(
                [executable, *args],
                env=self._build_env(),
                input=input_text,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise DatabaseError(f"Could not run {tool}: {exc}") from exc
        log.debug(
            "%s exited with code %s in %.2fs", tool, result.returncode, time.monotonic() - started
        )
        if result.returncode != 0 and result.stderr:
            log.debug("%s stderr: %s", tool, result.stderr.strip())
        return result

    def _psql(self, sql: str, name: str) -> str:
        """Run one SQL statement against the maintenance database.

        ``name`` is passed to psql as a *variable*; the SQL refers to it as
        :'name' (quoted as a string) or :"name" (quoted as an identifier), so
        psql does the quoting and a strange database name cannot inject SQL.
        The statement goes in through stdin because psql only substitutes
        variables there, not inside -c.
        """
        result = self._run(
            "psql",
            [
                *self._connection_args(self._MAINTENANCE_DB),
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "-v", "ON_ERROR_STOP=1",
                "-v", f"name={name}",
            ],
            input_text=sql,
        )
        if result.returncode != 0:
            raise DatabaseError(self._explain_failure(result.stderr, "psql"))
        return result.stdout.strip()

    # ------------------------------------------------------------- backup

    def backup(self, output_path: Path) -> None:
        output_path = Path(output_path)
        result = self._run(
            "pg_dump",
            ["--format=custom", *self._connection_args(), f"--file={output_path}"],
        )
        if result.returncode != 0:
            # Don't leave a half-written file behind.
            output_path.unlink(missing_ok=True)
            raise DatabaseError(self._explain_failure(result.stderr, "pg_dump"))

    # ------------------------------------------------------------ restore

    def verify_backup(self, input_path: Path) -> None:
        # --list only reads the file's table of contents. If that works, the
        # file is a complete, readable custom-format archive.
        result = self._run("pg_restore", ["--list", str(input_path)])
        if result.returncode != 0:
            raise DatabaseError(self._explain_failure(result.stderr, "pg_restore"))

    def database_exists(self) -> bool:
        answer = self._psql(
            "SELECT 1 FROM pg_database WHERE datname = :'name';", self.config.db_name
        )
        return answer == "1"

    def create_database(self) -> None:
        self._psql('CREATE DATABASE :"name";', self.config.db_name)

    def restore(self, input_path: Path, *, clean: bool = False) -> None:
        args = [
            # One transaction: if anything fails, everything is rolled back
            # and the database is exactly as it was before.
            "--single-transaction",
            # Don't try to recreate the original owner roles, which may not
            # exist on this server. Objects belong to the user restoring.
            "--no-owner",
            *self._connection_args(),
        ]
        if clean:
            args += ["--clean", "--if-exists"]
        args.append(str(input_path))

        result = self._run("pg_restore", args)
        if result.returncode != 0:
            raise DatabaseError(self._explain_failure(result.stderr, "pg_restore"))

    # ------------------------------------------------------------- errors

    @staticmethod
    def _explain_failure(stderr: str, tool: str) -> str:
        text = stderr.strip() or f"{tool} failed with no error message."
        lowered = text.lower()

        if "server version mismatch" in lowered:
            return (
                "Your pg_dump is older than the database server. Install PostgreSQL "
                "client tools of the same version or newer.\n\n" + text
            )
        if "unsupported version" in lowered:
            return (
                "This backup was made with newer PostgreSQL tools than your "
                "pg_restore. Install PostgreSQL client tools of the same version "
                "or newer.\n\n" + text
            )
        if "valid archive" in lowered or "text format dump" in lowered:
            return (
                "This file is not a valid backup made by this tool (it may be "
                "corrupt, incomplete, or a different kind of file).\n\n" + text
            )
        if "password" in lowered and (
            "no password supplied" in lowered or "authentication failed" in lowered
        ):
            return (
                "Could not log in to the database. Check the user name and password. "
                "(Set the password with the YOURTOOL_DB_PASSWORD environment "
                "variable, or run interactively to be prompted.)\n\n" + text
            )
        if tool == "pg_restore" and "already exists" in lowered:
            return (
                "The target database already contains objects with the same names, "
                "so nothing was restored and nothing was changed. Use --clean to "
                "replace them, or restore into a new database name.\n\n" + text
            )
        return text
