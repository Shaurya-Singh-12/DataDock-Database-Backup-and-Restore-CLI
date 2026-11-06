import logging
import os
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterator, NoReturn, Optional

import typer

from yourtool import __version__
from yourtool.backup import run_backup
from yourtool.config import REQUIRED, Config, ConfigError, resolve_config
from yourtool.databases import DatabaseError, get_adapter_class
from yourtool.integrity import verify_stored_backup
from yourtool.jobs import execute_job
from yourtool.schedule import Schedule, ScheduleError, load_schedule
from yourtool.scheduler import Scheduler
from yourtool.restore import RestoreCancelled, RestorePlan, run_restore
from yourtool.retention import Policy, PrunePlan, build_plan, execute_prune, stored_backups
from yourtool.storage import BackupInfo, IntegrityError, StorageAdapter, StorageError, get_storage
from yourtool.utils.formatting import format_size
from yourtool.utils.logsetup import add_secret, configure_logging, enable_file_logging
from yourtool.utils.naming import parse_backup_filename

log = logging.getLogger(__name__)

# Exit codes (documented in the README):
EXIT_FAILURE = 1  # the operation failed
EXIT_USAGE = 2  # bad command line (Typer uses this itself)
EXIT_INTERRUPTED = 130  # Ctrl+C, the usual convention on Linux/macOS

app = typer.Typer(
    help="Back up and restore databases from the command line.",
    no_args_is_help=True,
)

config_app = typer.Typer(help="Inspect configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")

schedule_app = typer.Typer(help="Run backups and other jobs on a schedule.", no_args_is_help=True)
app.add_typer(schedule_app, name="schedule")

# Options shared by every command that needs to know which database to use.
ConfigFileOpt = Annotated[
    Optional[Path], typer.Option("--config", "-c", help="Path to a YAML config file.")
]
DbTypeOpt = Annotated[Optional[str], typer.Option("--db-type", help="Database type.")]
DbHostOpt = Annotated[Optional[str], typer.Option("--db-host", help="Database host.")]
DbPortOpt = Annotated[Optional[int], typer.Option("--db-port", help="Database port.")]
DbUserOpt = Annotated[Optional[str], typer.Option("--db-user", help="Database user.")]
DbNameOpt = Annotated[Optional[str], typer.Option("--db-name", help="Database name.")]
# Options that say where backups are kept.
StorageOpt = Annotated[Optional[str], typer.Option("--storage", help="Storage type: local or s3.")]
PathOpt = Annotated[
    Optional[str], typer.Option("--path", help="Backup folder (local storage).")
]
BucketOpt = Annotated[Optional[str], typer.Option("--bucket", help="S3 bucket name.")]
PrefixOpt = Annotated[
    Optional[str], typer.Option("--prefix", help="Folder inside the S3 bucket, e.g. backups/prod.")
]
RegionOpt = Annotated[Optional[str], typer.Option("--region", help="S3 region, e.g. eu-west-1.")]
EndpointOpt = Annotated[
    Optional[str],
    typer.Option("--endpoint-url", help="Address of an S3-compatible service (not needed for Amazon)."),
]
CompressionOpt = Annotated[
    Optional[str],
    typer.Option("--compression", help="Compress the backup: none or gzip. Default: none."),
]
KeepLastOpt = Annotated[
    Optional[int], typer.Option("--keep-last", help="Keep the N most recent backups.")
]
KeepDailyOpt = Annotated[
    Optional[int],
    typer.Option("--keep-daily", help="Keep the newest backup of each of the last N days that have one."),
]
KeepWeeklyOpt = Annotated[
    Optional[int],
    typer.Option("--keep-weekly", help="Keep the newest backup of each of the last N weeks that have one."),
]
KeepMonthlyOpt = Annotated[
    Optional[int],
    typer.Option("--keep-monthly", help="Keep the newest backup of each of the last N months that have one."),
]
NoPromptOpt = Annotated[
    bool,
    typer.Option(
        "--no-prompt",
        help="Never ask for the password (use for scripts, or with a .pgpass file).",
    ),
]
VerboseOpt = Annotated[
    bool,
    typer.Option("--verbose", "-v", help="Show detailed progress, including each command run."),
]
QuietOpt = Annotated[
    bool,
    typer.Option("--quiet", "-q", help="Print only errors. Success is silent (exit code 0)."),
]
LogFileOpt = Annotated[
    Optional[str],
    typer.Option(
        "--log-file",
        help="Also write a log to this file (rotated at about 1 MB). Off by default.",
    ),
]


# ---------------------------------------------------------------- helpers


def _fail(message: object, *, exc_info: bool = False) -> NoReturn:
    """Log an error (shown on the terminal and written to the log file) and exit 1."""
    log.error("%s", message, exc_info=exc_info)
    raise typer.Exit(code=EXIT_FAILURE)


def _say(text: str, quiet: bool, *, fg: Optional[str] = None) -> None:
    """Normal output. Silenced by --quiet; errors never are."""
    if not quiet:
        typer.secho(text, fg=fg)


def _is_interactive() -> bool:
    """True when a person is at the keyboard (not cron, a pipe, or CI)."""
    return sys.stdin.isatty()


def _start_logging(
    verbose: bool, quiet: bool, *, info: bool = False, timestamps: bool = False
) -> None:
    if verbose and quiet:
        typer.secho("Error: Use either --verbose or --quiet, not both.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)
    configure_logging(verbose=verbose, quiet=quiet, info=info, timestamps=timestamps)


def _load_config(
    config_file: Optional[Path], *, required: Optional[tuple] = None, **cli_values: object
) -> tuple[Config, dict]:
    try:
        return resolve_config(
            cli=cli_values,
            config_file=config_file,
            required=REQUIRED if required is None else required,
        )
    except ConfigError as exc:
        _fail(exc)


def _register_secrets(config: Config) -> None:
    """Tell the logger which values must never be printed or written."""
    add_secret(config.db_password)
    # Password variables the database tools read, and the AWS secret keys.
    for variable in ("PGPASSWORD", "MYSQL_PWD", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        add_secret(os.environ.get(variable))


def _begin_run(command: str, config: Config, verbose: bool) -> None:
    if config.log_file:
        enable_file_logging(config.log_file, verbose=verbose)
    log.info("yourtool %s: %s", __version__, command)
    if config.storage_type == "s3":
        where = f"s3 bucket={config.storage_bucket} prefix={config.storage_prefix or '-'}"
    else:
        where = f"local path={config.storage_path}"
    log.debug(
        "Settings: database=%s host=%s port=%s user=%s storage=%s compression=%s",
        config.db_name, config.db_host, config.db_port, config.db_user, where,
        config.compression,
    )


def _needs_password_prompt(config: Config, no_prompt: bool) -> bool:
    # Each database has its own standard variable (PGPASSWORD, MYSQL_PWD, ...).
    # If it is already set, the person has supplied the password and we don't ask.
    try:
        standard_variable = get_adapter_class(config.db_type).password_env_var
    except DatabaseError:
        standard_variable = None
    return (
        not no_prompt
        and config.db_password is None
        and (standard_variable is None or standard_variable not in os.environ)
        and _is_interactive()
    )


def _with_password(config: Config, no_prompt: bool) -> Config:
    """Ask for the password if it wasn't supplied and a person is present."""
    if _needs_password_prompt(config, no_prompt):
        password = typer.prompt(
            f"Password for {config.db_user}@{config.db_host}",
            hide_input=True,
            default="",
            show_default=False,
        )
        config = replace(config, db_password=password or None)
    _register_secrets(config)
    return config


@contextmanager
def _guard(interrupted_message: str) -> Iterator[None]:
    """Turn every way a command can fail into a clean message and exit code."""
    try:
        yield
    except (typer.Exit, typer.Abort):
        raise  # already handled / a prompt was cancelled; Typer deals with these
    except (DatabaseError, StorageError, RestoreCancelled) as exc:
        _fail(exc)  # expected problems: a clear message, no traceback
    except KeyboardInterrupt:
        log.warning("%s", interrupted_message)
        raise typer.Exit(code=EXIT_INTERRUPTED)
    except Exception as exc:  # a bug, or something we did not anticipate
        _fail(
            f"Unexpected error ({type(exc).__name__}): {exc}. "
            "Run again with --verbose to see the full details.",
            exc_info=True,
        )


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"yourtool {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """Back up and restore databases from the command line."""
    configure_logging()  # sensible defaults; commands adjust them below


# ------------------------------------------------------------------ backup


@app.command()
def backup(
    config_file: ConfigFileOpt = None,
    db_type: DbTypeOpt = None,
    db_host: DbHostOpt = None,
    db_port: DbPortOpt = None,
    db_user: DbUserOpt = None,
    db_name: DbNameOpt = None,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    compression: CompressionOpt = None,
    no_prompt: NoPromptOpt = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Create a backup of a database.

    The password is never accepted as a flag. Set YOURTOOL_DB_PASSWORD, or
    you will be asked for it.

    Exit codes: 0 success, 1 failure, 2 bad command line, 130 interrupted.
    """
    _start_logging(verbose, quiet)
    config, _ = _load_config(
        config_file,
        db_type=db_type,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_name=db_name,
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        compression=compression,
        log_file=log_file,
    )
    _register_secrets(config)
    _begin_run("backup", config, verbose)
    config = _with_password(config, no_prompt)

    with _guard("Interrupted. No backup was stored."):
        _say(f"Backing up '{config.db_name}' from {config.db_host}:{config.db_port} ...", quiet)
        result = run_backup(config)

    _say(
        f"Backup complete: {result.location} ({format_size(result.size)})",
        quiet,
        fg=typer.colors.GREEN,
    )
    details = []
    if result.compression != "none" and result.original_size:
        details.append(
            f"{result.compression}: {format_size(result.original_size)} -> {format_size(result.size)}"
        )
    if result.sha256:
        details.append(f"sha256 {result.sha256[:16]}…")
    if details:
        _say("  " + "   ".join(details), quiet)


# ------------------------------------------------------------ list backups


_stored_backups = stored_backups  # shared with the scheduler's jobs


def _print_backups(choices: list[tuple[BackupInfo, datetime]]) -> None:
    for number, (info, when) in enumerate(choices, start=1):
        typer.echo(
            f"  {number:>2}) {info.name}   {format_size(info.size)}   "
            f"{when:%Y-%m-%d %H:%M:%S} UTC"
        )


@app.command("list")
def list_backups(
    config_file: ConfigFileOpt = None,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    names_only: Annotated[
        bool,
        typer.Option("--names-only", help="Print just the file names, one per line (for scripts)."),
    ] = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """List the backups in storage, newest first.

    Needs no database settings. The list itself is the result, so --quiet does
    not hide it.
    """
    _start_logging(verbose, quiet)
    config, _ = _load_config(
        config_file,
        required=(),
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        log_file=log_file,
    )
    _register_secrets(config)
    _begin_run("list", config, verbose)

    with _guard("Interrupted."):
        storage = get_storage(config)
        choices = _stored_backups(storage)

    if names_only:
        for info, _ in choices:
            typer.echo(info.name)
    elif not choices:
        typer.echo(f"No backups found in {storage.describe()}.")
    else:
        typer.echo(f"Backups in {storage.describe()} (newest first):")
        _print_backups(choices)


# ------------------------------------------------------------------ verify


@app.command()
def verify(
    config_file: ConfigFileOpt = None,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    backup_file: Annotated[
        Optional[str], typer.Option("--file", help="Name of the backup to check.")
    ] = None,
    latest: Annotated[
        bool, typer.Option("--latest", help="Check the newest backup (the default).")
    ] = False,
    all_backups: Annotated[
        bool, typer.Option("--all", help="Check every stored backup.")
    ] = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Check stored backups against their checksums, without restoring anything.

    This proves a backup has not been damaged or changed since it was made.
    It does not need the database. With no options, the newest backup is checked.

    Exit codes: 0 all fine, 1 a backup failed its check, 2 bad command line.
    """
    _start_logging(verbose, quiet)
    if sum([bool(backup_file), latest, all_backups]) > 1:
        typer.secho(
            "Error: Use only one of --file, --latest and --all.", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(code=EXIT_USAGE)

    config, _ = _load_config(
        config_file,
        required=(),
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        log_file=log_file,
    )
    _register_secrets(config)
    _begin_run("verify", config, verbose)

    outcomes: list[tuple[str, str, str]] = []  # (name, status, detail)
    with _guard("Interrupted."):
        storage = get_storage(config)
        if backup_file:
            names = [backup_file]
        else:
            choices = _stored_backups(storage)
            if not choices:
                _fail("No backups found. Run 'yourtool backup' first.")
            names = [c[0].name for c in choices] if all_backups else [choices[0][0].name]

        for name in names:
            with tempfile.TemporaryDirectory(prefix="yourtool-") as tmp:
                try:
                    result = verify_stored_backup(storage, name, Path(tmp))
                except (IntegrityError, StorageError) as exc:
                    outcomes.append((name, "failed", str(exc)))
                else:
                    if result.status == "ok":
                        outcomes.append((name, "ok", f"sha256 {result.sha256[:16]}…"))
                    else:
                        outcomes.append((name, "unverified", "no checksum was stored"))

    failed = 0
    for name, status, detail in outcomes:
        if status == "ok":
            _say(f"  OK           {name}   {detail}", quiet, fg=typer.colors.GREEN)
        elif status == "unverified":
            _say(f"  NO CHECKSUM  {name}   ({detail}; cannot be verified)", quiet, fg=typer.colors.YELLOW)
        else:
            failed += 1
            typer.secho(f"  FAILED       {name}", fg=typer.colors.RED)
            typer.secho(f"               {detail}", fg=typer.colors.RED)
    counts = {k: sum(1 for _, st, _ in outcomes if st == k) for k in ("ok", "unverified", "failed")}
    _say(
        f"Checked {len(outcomes)} backup(s): {counts['ok']} ok, "
        f"{counts['unverified']} without checksum, {counts['failed']} failed.",
        quiet,
    )
    if failed:
        raise typer.Exit(code=EXIT_FAILURE)


# ------------------------------------------------------------------- prune


def _print_prune_plan(plan: PrunePlan, policy: Policy, where: str) -> None:
    typer.echo(f"Backups in {where}")
    typer.echo(f"Rules: keep {policy.describe()}")
    for database, decisions in plan.decisions.items():
        typer.echo("")
        typer.echo(f"Database '{database}': {len(decisions)} backup(s), newest first")
        for decision in decisions:
            backup = decision.backup
            size = format_size(backup.size)
            if decision.keep:
                typer.secho(
                    f"  KEEP    {backup.name}   {size}   ({', '.join(decision.reasons)})",
                    fg=typer.colors.GREEN,
                )
            else:
                extra = " + its checksum file" if backup.checksum else ""
                typer.secho(f"  DELETE  {backup.name}   {size}{extra}", fg=typer.colors.RED)
    if plan.orphan_checksums:
        typer.echo("")
        typer.echo(
            f"Note: {len(plan.orphan_checksums)} checksum file(s) have no matching backup "
            "and are left alone."
        )
    typer.echo("")


@app.command()
def prune(
    config_file: ConfigFileOpt = None,
    db_name: DbNameOpt = None,
    all_databases: Annotated[
        bool,
        typer.Option("--all-databases", help="Apply the rules to every database found in storage."),
    ] = False,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    keep_last: KeepLastOpt = None,
    keep_daily: KeepDailyOpt = None,
    keep_weekly: KeepWeeklyOpt = None,
    keep_monthly: KeepMonthlyOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show what would be deleted, and delete nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Delete without asking (for scripts).")
    ] = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Delete old backups according to retention rules. Asks before deleting.

    A backup is kept if ANY rule keeps it. The newest backup is never deleted.
    Only backups of one database are considered (the one in your config, or
    --db-name), unless you pass --all-databases. Files this tool did not create
    are never touched. Use --dry-run to preview.

    Exit codes: 0 done (or nothing to do), 1 failed or cancelled, 2 bad command line.
    """
    _start_logging(verbose, quiet)
    if db_name and all_databases:
        typer.secho("Error: Use either --db-name or --all-databases, not both.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)
    if dry_run and yes:
        typer.secho("Error: --dry-run deletes nothing, so --yes makes no sense with it.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)

    config, _ = _load_config(
        config_file,
        required=(),
        db_name=db_name,
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        keep_last=keep_last,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        log_file=log_file,
    )
    policy = Policy(config.keep_last, config.keep_daily, config.keep_weekly, config.keep_monthly)
    if policy.is_empty():
        _fail(
            "No retention rule was given, so nothing can be decided. Use --keep-last N "
            "(or --keep-daily / --keep-weekly / --keep-monthly), or set them under "
            "'retention:' in the config file. Nothing was deleted."
        )
    if not all_databases and not config.db_name:
        _fail(
            "Say which database's backups to prune: --db-name NAME (or 'name:' under "
            "'database:' in the config file), or --all-databases. Nothing was deleted."
        )
    _register_secrets(config)
    _begin_run("prune", config, verbose)
    log.info("Retention rules: keep %s", policy.describe())

    with _guard("Interrupted. Backups already deleted stay deleted; nothing further was deleted."):
        storage = get_storage(config)
        plan = build_plan(
            storage.list(), database=None if all_databases else config.db_name, policy=policy
        )
        scope = "any database" if all_databases else f"database '{config.db_name}'"
        if not plan.decisions:
            typer.echo(f"No backups of {scope} found in {storage.describe()}.")
            return

        doomed = plan.to_delete()
        will_prompt = bool(doomed) and not yes and not dry_run and _is_interactive()
        if dry_run or will_prompt or not quiet:
            _print_prune_plan(plan, policy, storage.describe())

        if not doomed:
            _say(f"Nothing to delete: all {plan.total_count()} backup(s) are kept by the rules.", quiet)
            return
        freed = sum(b.size + b.checksum_size for b in doomed)
        summary = (
            f"Keeping {plan.kept_count()} of {plan.total_count()} backup(s). "
            f"Deleting {len(doomed)} ({format_size(freed)})."
        )
        if dry_run:
            typer.echo(summary)
            typer.echo("Dry run: nothing was deleted.")
            return
        if will_prompt or not quiet:
            typer.echo(summary)

        if not yes:
            if not _is_interactive():
                _fail(
                    "Refusing to delete without confirmation when not running interactively. "
                    "Use --dry-run to preview, or --yes to delete."
                )
            typed = typer.prompt(
                f"Type 'delete' to permanently delete these {len(doomed)} backup(s)",
                default="",
                show_default=False,
            )
            if typed != "delete":
                _fail("Cancelled. Nothing was deleted.")

        result = execute_prune(storage, doomed)

    _say(
        f"Deleted {len(result.deleted)} backup(s), freed {format_size(result.freed)}. "
        f"Kept {plan.kept_count()}.",
        quiet,
        fg=typer.colors.GREEN,
    )
    if result.failed:
        for backup, reason in result.failed:
            typer.secho(f"  FAILED to delete {backup.name}: {reason}", fg=typer.colors.RED)
        _fail(f"{len(result.failed)} backup(s) could not be deleted (see above).")


# ----------------------------------------------------------------- restore


def _choose_backup(storage: StorageAdapter, *, latest: bool = False) -> str:
    """Pick the backup to restore: the newest, or one the person chooses by number."""
    choices = _stored_backups(storage)

    if not choices:
        _fail(
            "No backups found. Run 'yourtool backup' first, or use --file to "
            "name a backup explicitly."
        )
    if latest:
        return choices[0][0].name
    if not _is_interactive():
        _fail(
            "Not running interactively. Use --file NAME or --latest to choose "
            "the backup to restore."
        )

    typer.echo("Available backups (newest first):")
    _print_backups(choices)
    while True:
        picked = typer.prompt("Which backup do you want to restore?", type=int, default=1)
        if 1 <= picked <= len(choices):
            return choices[picked - 1][0].name
        typer.echo(f"Please enter a number between 1 and {len(choices)}.")


def _make_confirm(yes: bool, quiet: bool) -> Callable[[RestorePlan], bool]:
    """Build the safety check that run_restore calls before changing anything."""

    def confirm(plan: RestorePlan) -> bool:
        will_prompt = plan.target_exists and not yes and _is_interactive()
        # --quiet hides the summary, except when we are about to ask a person
        # to confirm: they must see what they are confirming.
        show_summary = will_prompt or not quiet

        if show_summary:
            typer.echo("")
            typer.echo(f"  Backup : {plan.backup_name}")
            typer.echo(f"  Target : database '{plan.target_db}' on {plan.host}:{plan.port}")
            if plan.integrity:
                typer.echo(f"  Check  : {plan.integrity}")

        if not plan.target_exists:
            # Nothing can be overwritten, so no confirmation is needed.
            if show_summary:
                typer.echo("  Action : the database does not exist yet and will be created")
                typer.echo("")
            return True

        if show_summary:
            if plan.clean:
                action = (
                    "objects that are in the backup are dropped and recreated "
                    "(other objects are left alone)"
                )
            else:
                action = (
                    "restore into the existing database "
                    "(if any names clash, it fails and changes nothing)"
                )
            typer.echo(f"  Action : {action}")
            typer.echo("")

        if yes:
            return True
        if not _is_interactive():
            raise RestoreCancelled(
                "The target database already exists. Re-run with --yes to confirm "
                "when not running interactively."
            )
        typed = typer.prompt(
            f"WARNING: this changes the existing database. Type '{plan.target_db}' to confirm",
            default="",
            show_default=False,
        )
        return typed == plan.target_db

    return confirm


@app.command()
def restore(
    target_db: Annotated[
        str,
        typer.Option(
            "--target-db",
            help="Database to restore INTO. Always required, never guessed. "
            "It is created if it does not exist.",
        ),
    ],
    config_file: ConfigFileOpt = None,
    db_type: DbTypeOpt = None,
    db_host: DbHostOpt = None,
    db_port: DbPortOpt = None,
    db_user: DbUserOpt = None,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    backup_file: Annotated[
        Optional[str],
        typer.Option("--file", help="Name of the backup to restore. Omit to pick from a list."),
    ] = None,
    latest: Annotated[
        bool,
        typer.Option("--latest", help="Restore the newest backup (no list, good for scripts)."),
    ] = False,
    clean: Annotated[
        bool,
        typer.Option(
            "--clean",
            help="Drop objects that are in the backup before recreating them. "
            "Objects not in the backup are left alone.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip the confirmation (for scripts)."),
    ] = False,
    no_prompt: NoPromptOpt = False,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Restore a backup into a database.

    Restoring into an existing database asks you to type its name first.
    Where the database allows it, the restore is all-or-nothing.

    Exit codes: 0 success, 1 failure or cancelled, 2 bad command line, 130 interrupted.
    """
    _start_logging(verbose, quiet)
    if backup_file and latest:
        typer.secho("Error: Use either --file or --latest, not both.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=EXIT_USAGE)

    # --target-db is passed in as the database name, so it wins over any
    # database name in the config file or environment.
    config, _ = _load_config(
        config_file,
        db_type=db_type,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_name=target_db,
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        log_file=log_file,
    )
    _register_secrets(config)
    _begin_run("restore", config, verbose)
    config = _with_password(config, no_prompt)

    with _guard(
        "Interrupted. A restore runs as a single transaction where the database "
        "supports it, so the database should be unchanged."
    ):
        name = backup_file or _choose_backup(get_storage(config), latest=latest)
        result = run_restore(config, name, clean=clean, confirm=_make_confirm(yes, quiet))

    verb = "created and restored" if result.created_database else "restored"
    _say(
        f"Restore complete: '{result.backup_name}' {verb} into database "
        f"'{result.target_db}'.",
        quiet,
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------- schedule

# The scheduler waits on this. SIGTERM (what `docker stop` sends) sets it, and the
# scheduler finishes the job it is running and then stops. Tests replace _clock/_wait.
_STOP = threading.Event()


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _wait(seconds: float) -> bool:
    return _STOP.wait(seconds)


def _install_stop_signals() -> None:
    def request_stop(signum, frame):  # only sets a flag: safe inside a signal handler
        _STOP.set()

    for name in ("SIGTERM", "SIGBREAK"):  # SIGBREAK is Ctrl+Break on Windows
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, request_stop)
            except (ValueError, OSError):
                pass  # not the main thread, or not supported here


def _load_schedule_or_fail(config_file: Optional[Path]) -> Schedule:
    if config_file is None:
        _fail(
            "The schedule lives in a config file. Give it with --config PATH "
            "(it needs a 'schedule:' section; see the README, 'Scheduling')."
        )
    try:
        return load_schedule(config_file)
    except ConfigError as exc:  # includes ScheduleError and CronError
        _fail(exc)


def _base_config(config_file: Path, log_file: Optional[str]) -> Config:
    config, _ = _load_config(config_file, required=(), log_file=log_file)
    return config


@schedule_app.command("list")
def schedule_list(
    config_file: ConfigFileOpt = None,
    count: Annotated[int, typer.Option("--next", help="How many upcoming run times to show per job.")] = 3,
) -> None:
    """Show the jobs and when each will next run. Nothing is executed."""
    schedule = _load_schedule_or_fail(config_file)
    now = _clock()
    typer.echo(f"Time zone: {schedule.timezone_name}   (a run may start up to "
               f"{int(schedule.misfire_grace.total_seconds() // 60)} minutes late)")
    for job in schedule.jobs:
        typer.echo("")
        extra = " (deletes without asking)" if job.command == "prune" else ""
        typer.echo(f"{job.name}   {job.command}{extra}   cron \"{job.cron.text}\"")
        for when in job.cron.next_runs(now, schedule.tz, max(count, 1)):
            utc = when.astimezone(timezone.utc)
            typer.echo(f"    next: {when:%a %Y-%m-%d %H:%M %Z}   ({utc:%H:%M} UTC)")


@schedule_app.command("run-now")
def schedule_run_now(
    name: Annotated[str, typer.Argument(help="Name of the job to run once, right now.")],
    config_file: ConfigFileOpt = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Run one job immediately, exactly as the scheduler would. Good for testing a job.

    Exit codes: 0 the job succeeded, 1 it failed.
    """
    _start_logging(verbose, quiet)
    schedule = _load_schedule_or_fail(config_file)
    try:
        job = schedule.job(name)
    except ScheduleError as exc:
        _fail(exc)
    base = _base_config(config_file, log_file)
    _register_secrets(job.config)
    _begin_run("schedule run-now", base, verbose)
    with _guard("Interrupted."):
        _say(f"Running job '{job.name}' ({job.command}) ...", quiet)
        outcome = execute_job(job)
    if not outcome.ok:
        _fail(f"Job '{job.name}' failed: {outcome.message}")
    _say(f"Job '{job.name}' finished OK: {outcome.message}", quiet, fg=typer.colors.GREEN)


@schedule_app.command("run")
def schedule_run(
    config_file: ConfigFileOpt = None,
    max_runs: Annotated[
        Optional[int],
        typer.Option("--max-runs", min=1, help="Stop after this many job runs (handy for testing)."),
    ] = None,
    verbose: VerboseOpt = False,
    quiet: QuietOpt = False,
    log_file: LogFileOpt = None,
) -> None:
    """Run the scheduled jobs until stopped (Ctrl+C, or SIGTERM/`docker stop`).

    Jobs run one at a time. If you stop it while a job is running, the job is
    allowed to finish first (Ctrl+C interrupts it instead).

    Exit codes: 0 stopped cleanly, 1 the schedule is invalid, 130 interrupted with Ctrl+C.
    """
    # A scheduler is long-running: show progress and timestamps on the terminal. This is
    # configured first, once, so nothing can reset the logging after secrets are registered.
    _start_logging(verbose, quiet, info=True, timestamps=True)
    schedule = _load_schedule_or_fail(config_file)
    base = _base_config(config_file, log_file)
    for job in schedule.jobs:
        _register_secrets(job.config)
    _begin_run("schedule run", base, verbose)

    for job in schedule.jobs:
        if job.command in ("backup",) and not (
            job.config.db_password
            or (get_adapter_class(job.config.db_type).password_env_var or "") in os.environ
        ):
            log.warning(
                "Job '%s': no database password is set (YOURTOOL_DB_PASSWORD). The backup will "
                "fail unless the database allows passwordless login or you use a .pgpass file.",
                job.name,
            )

    _STOP.clear()
    _install_stop_signals()
    log.info(
        "Scheduler started: %d job(s), time zone %s. Press Ctrl+C to stop.",
        len(schedule.jobs), schedule.timezone_name,
    )
    scheduler = Scheduler(schedule, execute_job, clock=_clock, wait=_wait)
    try:
        runs = scheduler.run(_STOP.is_set, max_runs=max_runs)
    except KeyboardInterrupt:
        log.warning("Interrupted. The scheduler is stopping.")
        raise typer.Exit(code=EXIT_INTERRUPTED)
    log.info("Scheduler stopped after %d job run(s).", runs)


# ------------------------------------------------------------------ config


@config_app.command("show")
def config_show(
    config_file: ConfigFileOpt = None,
    db_type: DbTypeOpt = None,
    db_host: DbHostOpt = None,
    db_port: DbPortOpt = None,
    db_user: DbUserOpt = None,
    db_name: DbNameOpt = None,
    storage_type: StorageOpt = None,
    storage_path: PathOpt = None,
    bucket: BucketOpt = None,
    prefix: PrefixOpt = None,
    region: RegionOpt = None,
    endpoint_url: EndpointOpt = None,
    compression: CompressionOpt = None,
    keep_last: KeepLastOpt = None,
    keep_daily: KeepDailyOpt = None,
    keep_weekly: KeepWeeklyOpt = None,
    keep_monthly: KeepMonthlyOpt = None,
    log_file: LogFileOpt = None,
) -> None:
    """Show the final settings and where each value came from.

    The database password is never accepted as a flag. Set it with the
    YOURTOOL_DB_PASSWORD environment variable (or you will be prompted).
    """
    config, sources = _load_config(
        config_file,
        db_type=db_type,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_name=db_name,
        storage_type=storage_type,
        storage_path=storage_path,
        storage_bucket=bucket,
        storage_prefix=prefix,
        storage_region=region,
        storage_endpoint_url=endpoint_url,
        compression=compression,
        keep_last=keep_last,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        log_file=log_file,
    )

    shown = dict(vars(config))
    shown["db_password"] = "********" if config.db_password else None
    width = max(len(k) for k in shown)
    for key, value in shown.items():
        text = "(not set)" if value is None else value
        source = sources.get(key, "-")
        typer.echo(f"{key:<{width}} = {text}   [{source}]")
