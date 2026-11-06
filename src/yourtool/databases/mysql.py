"""MySQL / MariaDB adapter. Uses the official command-line tools
(mysqldump and mysql, or MariaDB's equivalents mariadb-dump and mariadb).

Differences from PostgreSQL worth knowing:

* The backup is a plain SQL text file (.sql), not a compressed archive.
* MySQL cannot undo a half-finished restore (its table-creating statements
  cannot be rolled back). So instead of "all or nothing" we protect you
  differently: an existing database must be empty unless you pass --clean,
  and a brand-new database that we created is removed again if the
  restore fails.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from yourtool.config import Config
from yourtool.databases.base import DatabaseAdapter, DatabaseError

log = logging.getLogger(__name__)

# MariaDB renamed its tools; either family works, so look for both.
_DUMP_TOOLS = ("mysqldump", "mariadb-dump")
_CLIENT_TOOLS = ("mysql", "mariadb")

# mysql prints "ERROR 1045 (28000): ..."; mysqldump prints "Got error: 1045: ...".
_ERROR_CODE = re.compile(r"(?:ERROR\s+|error:\s*)(\d{4})")
_DUMP_HEADER = re.compile(r"^-- (MySQL|MariaDB) dump", re.MULTILINE)
_DUMP_TRAILER = "-- Dump completed"

# Error numbers (the same in MySQL and MariaDB, and in every language).
_ACCESS_DENIED = 1045
_DB_ACCESS_DENIED = 1044
_UNKNOWN_DATABASE = 1049
_CANNOT_CONNECT = {2002, 2003, 2005, 2013}


def quote_identifier(name: str) -> str:
    """`my db` -> `\\`my db\\``. Backticks inside the name are doubled."""
    return "`" + name.replace("`", "``") + "`"


class MySqlAdapter(DatabaseAdapter):
    file_extension = ".sql"
    password_env_var = "MYSQL_PWD"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._created_database = False  # did we create the target during this run?

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _find_tool(names: tuple[str, ...]) -> str:
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        raise DatabaseError(
            f"{names[0]} was not found (also looked for {names[1]}). Install the MySQL "
            "or MariaDB command-line client tools and make sure their 'bin' folder "
            "is on your PATH."
        )

    def _db_name(self) -> str:
        name = self.config.db_name
        if name.startswith("-"):
            # It would be read as a command-line option. Refuse rather than risk it.
            raise DatabaseError(f"Database names starting with '-' are not supported: {name!r}")
        return name

    def _connection_args(self) -> list[str]:
        c = self.config
        return [
            f"--host={c.db_host}",
            f"--port={c.db_port}",
            f"--user={c.db_user}",
            # Without this, host "localhost" silently means "a local socket file"
            # and the port setting is ignored.
            "--protocol=tcp",
        ]

    def _write_option_file(self) -> str | None:
        """Put the password in a private temporary file for the MySQL tools.

        The tools read it from there, so it never appears in the command line
        (visible to other users) or in log output. The file is deleted
        immediately after the command finishes.
        """
        password = self.config.db_password
        if not password:
            return None
        if "\n" in password or "\r" in password:
            raise DatabaseError("The database password cannot contain line breaks.")
        escaped = password.replace("\\", "\\\\").replace('"', '\\"')
        fd, path = tempfile.mkstemp(prefix="yourtool-", suffix=".cnf")  # created private (0600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f'[client]\npassword="{escaped}"\n')
        return path

    def _run(
        self, executable: str, args: list[str], *, stdin_path: Path | None = None
    ) -> subprocess.CompletedProcess:
        option_file = self._write_option_file()
        # --defaults-extra-file must be the very first argument.
        command = [executable]
        if option_file:
            command.append(f"--defaults-extra-file={option_file}")
        command += args

        log.debug("Running: %s", shlex.join(command))
        started = time.monotonic()
        stdin_handle = None
        try:
            if stdin_path is not None:
                stdin_handle = open(stdin_path, "rb")  # streamed: a dump may be huge
            result = subprocess.run(
                command,
                env=os.environ.copy(),
                stdin=stdin_handle,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            raise DatabaseError(f"Could not run {Path(executable).name}: {exc}") from exc
        finally:
            if stdin_handle is not None:
                stdin_handle.close()
            if option_file:
                try:
                    os.unlink(option_file)
                except OSError:
                    pass
        log.debug(
            "%s exited with code %s in %.2fs",
            Path(executable).name, result.returncode, time.monotonic() - started,
        )
        if result.returncode != 0 and result.stderr:
            log.debug("%s stderr: %s", Path(executable).name, result.stderr.strip())
        return result

    def _mysql(
        self, args: list[str], *, database: str | None = None, stdin_path: Path | None = None
    ) -> subprocess.CompletedProcess:
        client = self._find_tool(_CLIENT_TOOLS)
        command_args = self._connection_args()
        if database is not None:
            command_args.append(f"--database={database}")
        return self._run(client, [*command_args, *args], stdin_path=stdin_path)

    # ------------------------------------------------------------- backup

    def backup(self, output_path: Path) -> None:
        output_path = Path(output_path)
        dump = self._find_tool(_DUMP_TOOLS)
        args = [
            *self._connection_args(),
            "--single-transaction",  # a consistent snapshot, without locking tables
            "--routines",
            "--triggers",
            "--events",
            "--no-tablespaces",  # avoids needing the global PROCESS privilege
            "--default-character-set=utf8mb4",
            f"--result-file={output_path}",
            self._db_name(),
        ]
        result = self._run(dump, args)
        if result.returncode != 0:
            output_path.unlink(missing_ok=True)  # don't leave a half-written file
            raise DatabaseError(self._explain_failure(result.stderr, "mysqldump"))

    # ------------------------------------------------------------ restore

    def verify_backup(self, input_path: Path) -> None:
        path = Path(input_path)
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                head = handle.read(4096).decode("utf-8", errors="replace")
                handle.seek(max(0, size - 4096))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            raise DatabaseError(f"Could not read the backup file: {exc}") from exc

        if not _DUMP_HEADER.search(head):
            raise DatabaseError(
                "This file is not a valid MySQL/MariaDB backup made by this tool "
                "(it does not start like a mysqldump file). It may be a backup from "
                "a different kind of database."
            )
        # mysqldump writes this closing line only when it finished successfully.
        if _DUMP_TRAILER not in tail:
            raise DatabaseError(
                "This backup looks incomplete: it does not end the way a finished "
                "dump does, so it may have been cut off. It is not a valid backup to restore."
            )

    def database_exists(self) -> bool:
        result = self._mysql(
            ["--batch", "--skip-column-names", "--execute=SELECT 1"],
            database=self._db_name(),
        )
        if result.returncode == 0:
            return True
        if self._error_code(result.stderr) == _UNKNOWN_DATABASE:
            return False
        raise DatabaseError(self._explain_failure(result.stderr, "mysql"))

    def create_database(self) -> None:
        sql = f"CREATE DATABASE {quote_identifier(self._db_name())}"
        result = self._mysql([f"--execute={sql}"])
        if result.returncode != 0:
            raise DatabaseError(self._explain_failure(result.stderr, "mysql"))
        self._created_database = True

    def _is_empty(self) -> bool:
        # DATABASE() is the database we connected to, so no name is pasted into SQL.
        sql = (
            "SELECT (SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE())"
            " + (SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE())"
            " + (SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE())"
            " + (SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = DATABASE())"
        )
        result = self._mysql(
            ["--batch", "--skip-column-names", f"--execute={sql}"], database=self._db_name()
        )
        if result.returncode != 0:
            raise DatabaseError(self._explain_failure(result.stderr, "mysql"))
        return result.stdout.strip() == "0"

    def _drop_database_we_created(self) -> bool:
        sql = f"DROP DATABASE {quote_identifier(self._db_name())}"
        result = self._mysql([f"--execute={sql}"])
        if result.returncode != 0:
            log.warning("Could not remove the new database after the failure: %s", result.stderr.strip())
            return False
        return True

    def restore(self, input_path: Path, *, clean: bool = False) -> None:
        if not clean and not self._is_empty():
            raise DatabaseError(
                "The target database is not empty, so nothing was restored and nothing "
                "was changed. MySQL cannot undo a restore that fails half way, so use "
                "--clean to replace the objects that are in the backup, or restore into "
                "a new database name."
            )

        result = self._mysql(
            ["--default-character-set=utf8mb4"],
            database=self._db_name(),
            stdin_path=Path(input_path),
        )
        if result.returncode == 0:
            return

        message = self._explain_failure(result.stderr, "mysql")
        if self._created_database and self._drop_database_we_created():
            message += "\n\nThe new database was removed again, so nothing is left behind."
        else:
            message += (
                "\n\nMySQL cannot undo a restore that fails half way, so the database "
                "may be partly restored. Fix the problem and run the restore again with --clean."
            )
        raise DatabaseError(message)

    # ------------------------------------------------------------- errors

    @staticmethod
    def _error_code(stderr: str) -> int | None:
        match = _ERROR_CODE.search(stderr)
        return int(match.group(1)) if match else None

    @classmethod
    def _explain_failure(cls, stderr: str, tool: str) -> str:
        text = stderr.strip() or f"{tool} failed with no error message."
        code = cls._error_code(stderr)

        if code == _ACCESS_DENIED:
            return (
                "Could not log in to the database. Check the user name and password. "
                "(Set the password with the YOURTOOL_DB_PASSWORD environment "
                "variable, or run interactively to be prompted.)\n\n" + text
            )
        if code in _CANNOT_CONNECT:
            return (
                "Could not connect to the database server. Check the host and port, "
                "and that the server is running.\n\n" + text
            )
        if code == _UNKNOWN_DATABASE:
            return "That database does not exist on the server.\n\n" + text
        if code == _DB_ACCESS_DENIED:
            return (
                "This user is not allowed to use that database (or it does not "
                "exist). Check the database name and the user's permissions.\n\n" + text
            )
        return text
