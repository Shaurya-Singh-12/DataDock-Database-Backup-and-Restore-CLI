[![tests](https://github.com/Shaurya-Singh-12/DataDock-Database-Backup-and-Restore-CLI/actions/workflows/tests.yml/badge.svg)](https://github.com/Shaurya-Singh-12/DataDock-Database-Backup-and-Restore-CLI/actions/workflows/tests.yml)
# DataDock

A command-line tool that backs up and restores **PostgreSQL** and **MySQL/MariaDB** databases to
local folders or **Amazon S3** (and S3-compatible services).

- **Safe restores:** checksums are verified first, existing databases need a typed confirmation, and PostgreSQL restores are all-or-nothing.
- **Integrity:** every backup gets a SHA-256 checksum file; `yourtool verify` re-checks any stored backup.
- **Compression:** optional gzip (about 4x smaller for SQL text dumps).
- **Retention:** `yourtool prune` deletes old backups by keep-last/daily/weekly/monthly rules, previews first, and never deletes the newest backup.
- **Scheduling:** a built-in scheduler runs backups, prunes and verifications on cron-style schedules.
- **Careful with secrets:** passwords never appear on command lines or in logs.
- **Runs anywhere:** Windows, Linux and Docker; tested on Python 3.10 to 3.13.

For how it all fits together, see [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Development setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

yourtool --help
yourtool --version
pytest
```

## Usage

```bash
# Password comes from an environment variable, or you are prompted for it
export YOURTOOL_DB_PASSWORD='...'      # PowerShell: $env:YOURTOOL_DB_PASSWORD = '...'

yourtool backup --db-user postgres --db-name testdb
yourtool backup --config config.yaml   # see example-config.yaml
yourtool config show --config config.yaml
```

```bash
# Restore: pick a backup from a list, into a database you name explicitly
yourtool restore --config config.yaml --target-db testdb_copy

# Skip the list, and replace objects that already exist in the target
yourtool restore --config config.yaml --target-db testdb --file testdb_20261009_143022.dump --clean
```

`--target-db` is always required and is created if it does not exist.
Restoring into an existing database asks you to type its name to confirm
(`--yes` skips this). A restore is all-or-nothing: if it fails, the database
is left exactly as it was.

```bash
yourtool list                          # show the stored backups, newest first
yourtool restore --target-db mydb_copy --latest   # restore the newest one, no list
```

Each backup is saved as `<database>_<YYYYMMDD_HHMMSS>.<ext>` (UTC) in the backup folder
(`./backups` by default), together with a checksum file ending in `.sha256` (see below).

## Storing backups in S3

Backups can be kept in Amazon S3, or in any service that speaks the S3 protocol.

```bash
pip install "yourtool[s3]"        # adds boto3, the AWS library (the Docker image already has it)

yourtool backup  --config config.yaml --storage s3 --bucket my-backups --prefix postgres --region eu-west-1
yourtool list    --storage s3 --bucket my-backups --prefix postgres --region eu-west-1
yourtool restore --target-db mydb_copy --latest --storage s3 --bucket my-backups --prefix postgres
```

Or in the config file (flags and `YOURTOOL_STORAGE_BUCKET`, `_PREFIX`, `_REGION`, `_ENDPOINT_URL`
environment variables work too):

```yaml
storage:
  type: s3
  bucket: my-backups
  prefix: postgres            # a "folder" inside the bucket (optional)
  region: eu-west-1
  # endpoint_url: http://localhost:9000   # only for S3-compatible services, not Amazon
```

**Credentials are not part of this tool's settings.** boto3 finds them the standard way: the
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` environment variables, `~/.aws/credentials`
(`aws configure`), or an IAM role. Keep them out of config files.

**The bucket must already exist.** The tool never creates buckets.

**Permissions.** A minimal IAM policy (replace `BUCKET` and `PREFIX`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/*" },
    { "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET",
      "Condition": { "StringLike": { "s3:prefix": ["PREFIX/*"] } } }
  ]
}
```

Credentials that may only upload (`s3:PutObject` alone) also work for `backup`: the checks that need
read access are skipped. Such write-only credentials are a good safeguard for backup jobs.

Behaviour worth knowing:

- An upload is all-or-nothing: the object appears only when it is complete, and the stored size is
  checked afterwards. Large backups are uploaded in parts automatically.
- A backup is never overwritten: a second backup with the same name is refused.
- `list` shows only files this tool named (`<database>_<timestamp>.<ext>`) directly under the prefix.
- For a service other than Amazon, set `endpoint_url`. The tool then uses plain `host/bucket/key`
  addressing and turns off the extra integrity headers newer boto3 versions send, which many
  S3-compatible services reject. Amazon S3 itself keeps boto3's stronger defaults.
- The automated tests (and the Docker demo) run against a local S3-protocol server, not Amazon.

## Deleting old backups (retention)

Backups pile up. `yourtool prune` deletes old ones according to rules you choose, and it **asks first**.

```bash
yourtool prune --keep-last 7 --dry-run    # preview only: shows what would be kept and deleted
yourtool prune --keep-last 7              # shows the plan, then you must type "delete"
yourtool prune --keep-last 7 --yes        # no question (for scripts)
```

A backup is **kept if any rule keeps it**; everything else is deleted.

| Rule | Keeps |
|---|---|
| `--keep-last N` | the N most recent backups |
| `--keep-daily N` | the newest backup of each of the last N days that have one |
| `--keep-weekly N` | the newest backup of each of the last N weeks (ISO weeks) that have one |
| `--keep-monthly N` | the newest backup of each of the last N months that have one |

For example `--keep-last 3 --keep-daily 7 --keep-monthly 6` keeps the latest three backups, one per day
for the last seven days you made a backup, and one per month for six months. The preview says which rule
kept each backup. Rules can also live in the config file:

```yaml
retention:
  keep_last: 3
  keep_daily: 7
  keep_monthly: 6
```

Safety, by design:

- **Nothing is deleted without a preview and a typed confirmation.** Without a terminal and without `--yes`
  it refuses. `--dry-run` never deletes.
- **The newest backup of a database is never deleted,** whatever the rules say.
- **One database at a time.** Only the database in your config (or `--db-name`) is considered, unless you
  pass `--all-databases`; then each database is judged separately.
- **Only files this tool created are touched** (`<database>_<timestamp>.<ext>`). Other files, other
  folders and other S3 prefixes are never considered.
- **A backup's checksum file is deleted with it,** backup first, so a crash can never leave a backup
  without its checksum. Checksum files whose backup is already gone are reported and left alone.
- **Every deletion is logged** (use `--log-file`), so there is a record of what was removed.
- Times come from the UTC timestamp in the file name, not from file dates.

Deleting is permanent. `prune` does not check that your newest backup is good; run `yourtool verify` for that.
On S3 the credentials need `s3:ListBucket` and `s3:DeleteObject` (the policy above already has both).

## Scheduling

`yourtool schedule` runs jobs on cron-style schedules, with no cron or Task Scheduler needed. Put a
`schedule:` section in your config file:

```yaml
schedule:
  timezone: Europe/Berlin            # default: UTC
  jobs:
    - name: nightly-backup
      command: backup                # backup | prune | verify
      cron: "0 2 * * *"              # every day at 02:00
    - name: weekly-prune
      command: prune
      cron: "30 3 * * sun"           # Sundays at 03:30
      delete_without_asking: true    # REQUIRED for prune: nobody is there to confirm
      with: {keep_last: 7, keep_weekly: 4}
    - name: daily-check
      command: verify
      cron: "@daily"
    - name: shop-backup              # another database: override its settings
      command: backup
      cron: "0 3 * * *"
      with: {db_name: shop}
```

```bash
yourtool schedule list    --config config.yaml   # the jobs and their next run times (runs nothing)
yourtool schedule run-now nightly-backup --config config.yaml   # run one job once, to test it
yourtool schedule run     --config config.yaml   # run until stopped (Ctrl+C, or `docker stop`)
```

Cron fields are `minute hour day-of-month month day-of-week`; lists, ranges, steps (`*/15`), names
(`mon-fri`, `jan`) and macros (`@daily`, `@hourly`...) work. Everything is checked when the file is loaded,
so a mistake stops the scheduler at startup, not at 2 a.m.

How it behaves:

- **Jobs run one at a time**, never overlapping. A job that became due while another was running starts as
  soon as the scheduler is free, unless it is more than `misfire_grace_minutes` late (default 60); then that run
  is skipped and logged. There is **no catching up**: a computer that slept through a backup does not run it
  several times on waking.
- **A failing job is logged and the scheduler carries on.** Use `--log-file` for a record.
- **Deleting is opt-in.** A `prune` job refuses to load unless it says `delete_without_asking: true`, because
  a scheduled job cannot ask. To delete by hand with a confirmation, run `yourtool prune` yourself.
- **Time zones:** schedules use the zone you set. When clocks go forward, a run inside the skipped hour happens an
  hour later that day; when clocks go back, a run inside the repeated hour happens once.
- **Passwords** come from `YOURTOOL_DB_PASSWORD` (or the database's own mechanism), never from the schedule.
- **Run only one scheduler per storage location**, or you will get duplicate backups.
- The scheduler runs while its process runs. On Windows, start it at log-on with Task Scheduler (run
  `yourtool schedule run --config ...`); on Linux, use a systemd service; or use Docker (below), where
  `restart: unless-stopped` keeps it running.

In Docker: `docker compose up -d scheduler` runs the demo schedule (every minute), and
`docker compose logs -f scheduler` shows it working. `docker compose stop scheduler` stops it cleanly.

## Compression and checksums

**Checksums (always on).** Every backup is stored together with a small file, `<backup>.sha256`, in the
same format the standard `sha256sum` tool uses, so you can also check one by hand
(`sha256sum -c name.dump.sha256`). Before a restore changes anything, the tool checks the backup
against it and refuses if they differ. A checksum catches accidental damage: a failing disk, a
cut-off copy, a mistaken edit. It does **not** stop someone who can change both files on purpose
(that needs signing or encryption).

```bash
yourtool verify              # check the newest backup against its checksum
yourtool verify --all        # check every stored backup
yourtool verify --file NAME  # check one
```

`verify` needs no database. It exits with 1 if any backup fails, so it suits a scheduled job. Backups made
before checksums existed are reported as "no checksum" (not as failures) and can still be restored,
with a warning.

**Compression (optional, off by default).**

```bash
yourtool backup --compression gzip
```

or `compression: gzip` under `backup:` in the config file, or `YOURTOOL_COMPRESSION=gzip`. The result
is an ordinary `.gz` file that any tool can open, and restore decompresses it automatically.

Whether it helps depends on the database. Measured on 300,000 rows of order data:

| Database | Plain | With gzip |
|---|---|---|
| MySQL/MariaDB (plain SQL text) | 52.0 MB | 13.3 MB (about 4x smaller) |
| PostgreSQL (custom format) | 13.1 MB | 12.5 MB (about 5% smaller) |

PostgreSQL's backup format is already compressed, so the tool warns that gzip will save little there.
Your own data will give different numbers.

The checksum covers the file as stored (the compressed file). Compressed backups are reproducible: the
same data always gives the same bytes, because no timestamp or file name is stored inside.

## MySQL and MariaDB

Set the database type to `mysql`. It works with both MySQL and MariaDB servers, and the default
port becomes 3306.

```bash
yourtool backup --db-type mysql --db-user root --db-name shop
yourtool restore --db-type mysql --db-user root --target-db shop_copy
```

(or `type: mysql` under `database:` in the config file, or `YOURTOOL_DB_TYPE=mysql`.)

You need the MySQL or MariaDB command-line client tools installed (`mysqldump` and `mysql`, or
MariaDB's `mariadb-dump` and `mariadb`) with their `bin` folder on your PATH. The tool finds either
family automatically.

How it differs from PostgreSQL:

- **The backup is plain SQL** (`.sql`), readable in any text editor, not a compressed archive.
- **Consistent without locking.** Backups use a single transaction snapshot, so they are consistent
  and do not block other users (InnoDB tables). Tables, views, stored procedures, triggers and events
  are all included.
- **A restore cannot be undone half way.** MySQL cannot roll back the statements that create tables,
  so the tool protects you differently: restoring into an *existing* database is refused unless it is
  empty, or you pass `--clean` (which replaces the objects that are in the backup and leaves others
  alone). A brand-new database created for a restore is removed again if the restore fails.
  A failed `--clean` restore can leave the database partly restored; run it again.
- **Passwords never appear on a command line.** The password is placed in a private temporary file
  that the MySQL tools read, and the file is deleted straight after.
- **The port is always honoured.** The tool forces a network (TCP) connection, because MySQL tools
  otherwise ignore the port when the host is `localhost`.
- **TLS.** The tool uses your normal MySQL client configuration. For a server that needs a certificate,
  add it to a `[client]` section in `~/.my.cnf` (for example `ssl-ca=/path/to/ca.pem`).

## Output, logging and exit codes

| Flag | Effect |
|---|---|
| *(none)* | Progress and the result are printed; problems are shown as warnings/errors |
| `--verbose` / `-v` | Also shows detail on stderr: settings, every external command run, timings |
| `--quiet` / `-q` | Prints errors only. A successful run is silent |
| `--log-file PATH` | Also writes a log file (UTC timestamps, rotated at about 1 MB). Off by default |

The log file can also be set with `YOURTOOL_LOG_FILE` or, in the YAML config:

```yaml
logging:
  file: ./yourtool.log
```

The database password is never written to the terminal or a log file.

For scheduled runs, a good combination is:

```bash
yourtool backup --config config.yaml --no-prompt --quiet --log-file ./yourtool.log
```

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | The operation failed (or a restore was cancelled) |
| 2 | Bad command line (unknown option, missing required option) |
| 130 | Interrupted with Ctrl+C |

## Development and testing

```bash
pip install -e ".[dev]"
pytest
```

By default this runs the unit tests only. They need no database, finish in a
second or two, and ignore any `YOURTOOL_*` / `PGPASSWORD` variables set on your machine.

### Integration tests (against a real PostgreSQL)

These run the real `pg_dump` / `pg_restore` / `psql`. They are skipped unless you opt in.
You need the PostgreSQL command-line tools on your PATH and a server you can log in to.

```powershell
# PowerShell
$env:YOURTOOL_INTEGRATION = '1'
$env:YOURTOOL_DB_PASSWORD = 'your-password'
pytest
```

```bash
# bash
YOURTOOL_INTEGRATION=1 YOURTOOL_DB_PASSWORD='your-password' pytest
```

Optional settings, if your server is not `localhost`, port `5432`, user `postgres`:
`YOURTOOL_DB_HOST`, `YOURTOOL_DB_PORT`, `YOURTOOL_DB_USER`.

The tests create their own temporary databases (named `yt_...`) and drop them
afterwards. They never read or change your own databases. The login user must be
allowed to create databases (the `postgres` user is).

### Optional: a throwaway server with Docker

If you prefer not to use your installed PostgreSQL, Docker can provide one. Use the
same major version as your `pg_dump` (check with `pg_dump --version`). Port 5433 avoids
clashing with a server already using 5432:

```bash
docker run --name yourtool-pg -e POSTGRES_PASSWORD=postgres -p 5433:5432 -d postgres:16

# then point the tests at it (PowerShell: $env:NAME = 'value')
YOURTOOL_INTEGRATION=1 YOURTOOL_DB_PORT=5433 YOURTOOL_DB_PASSWORD=postgres pytest

docker rm -f yourtool-pg   # remove it when finished
```

### S3 tests

The S3 tests need no external service or account. They start a local S3-protocol server (moto) inside
the test run, and they are part of the normal `pytest` run once the dev extras are installed
(`pip install -e ".[dev]"`). Your real AWS credentials are never used: every test starts with all
`AWS_*` variables removed.

### Integration tests for MySQL / MariaDB

These need a MySQL or MariaDB server and the client tools, and have their own switch so that people
who only use PostgreSQL never see them fail.

```powershell
# PowerShell
$env:YOURTOOL_INTEGRATION_MYSQL = '1'
$env:YOURTOOL_MYSQL_USER = 'root'
$env:YOURTOOL_MYSQL_PASSWORD = 'your-password'
pytest
```

Optional: `YOURTOOL_MYSQL_HOST`, `YOURTOOL_MYSQL_PORT` (defaults `localhost`, `3306`). As with PostgreSQL,
the tests create and delete their own temporary databases. The user needs permission to create
databases and users.

The suite has been run against MySQL 8.0 and MariaDB 10.11.

### Coverage

```bash
pytest --cov=yourtool --cov-report=term-missing
```

### Continuous integration

`.github/workflows/tests.yml` runs the full suite, including the integration tests
against a PostgreSQL 16 container, on Python 3.10 and 3.13 for every push and pull
request. It starts working once the project is pushed to GitHub.

## Running with Docker

The repository includes a `Dockerfile` (the tool as an image) and a
`docker-compose.yml` (a ready-made demo: a PostgreSQL server with sample data,
plus the tool). The only thing you need installed is Docker.

```bash
# 1. Start the demo database and wait until it is ready
docker compose up -d --wait db

# 2. Back it up (backups are kept in a Docker volume called "backups")
docker compose run --rm yourtool backup

# 3. Restore into a NEW database. You will be shown a list to pick from.
docker compose run --rm yourtool restore --target-db demo_copy

# 4. Check the restored data
docker compose exec db psql -U postgres -d demo_copy -c "SELECT * FROM students;"

# See the backup files
docker compose run --rm --entrypoint ls yourtool -l /backups

# Remove everything (containers, the database and the backups volume)
docker compose down -v
```

Passing options works exactly as it does without Docker, for example
`docker compose run --rm yourtool backup --verbose`.

### The MariaDB demo

```bash
docker compose up -d --wait mariadb
docker compose run --rm yourtool-mysql backup        # gzip is on for this demo
docker compose run --rm yourtool-mysql verify
docker compose run --rm yourtool-mysql restore --target-db demo_copy --latest
docker compose exec mariadb mariadb -uroot -pdemo-password -e "SELECT * FROM demo_copy.students"
docker compose down -v
```

The tool image contains MariaDB's client, which also works with MySQL servers. The demo mounts
`docker/mariadb-client.cnf` to turn TLS off for the private demo network; do not do that for a
real server.

### The retention demo

```bash
docker compose up -d --wait db
docker compose run --rm yourtool backup
docker compose run --rm yourtool backup      # run it a second time, a few seconds later
docker compose run --rm yourtool prune --keep-last 1 --dry-run
docker compose run --rm yourtool prune --keep-last 1      # type "delete" when asked
docker compose run --rm yourtool list
```

### The S3 demo

A local S3 server (moto) stands in for Amazon, so no AWS account is needed:

```bash
docker compose up -d --wait db
docker compose up -d s3
docker compose run --rm --entrypoint python yourtool-s3 /create-bucket.py
docker compose run --rm yourtool-s3 backup
docker compose run --rm yourtool-s3 list
docker compose run --rm yourtool-s3 verify
docker compose run --rm yourtool-s3 restore --target-db demo_s3 --latest
docker compose exec db psql -U postgres -d demo_s3 -c "SELECT * FROM students;"
docker compose down -v
```

The demo server keeps everything in memory, so the bucket disappears when it stops.

### Using the image on its own

```bash
docker build -t yourtool .
docker run --rm yourtool --version

docker run --rm \
  -e YOURTOOL_DB_HOST=host.docker.internal \
  -e YOURTOOL_DB_USER=postgres \
  -e YOURTOOL_DB_PASSWORD='your-password' \
  -e YOURTOOL_DB_NAME=mydb \
  -v yourtool-backups:/backups \
  yourtool backup
```

(`host.docker.internal` means "the computer running Docker", which is how the
container reaches a PostgreSQL installed on your machine.)

Notes:

- **Match the PostgreSQL version.** `pg_dump` must be the same version as the server,
  or newer. The image installs version 16 by default. For another server version, use
  `docker build --build-arg PG_MAJOR=17 .` (with compose, set `PG_MAJOR=17` first).
- **Interactive prompts need a terminal.** `docker compose run` provides one. With
  plain `docker run`, add `-it` if you want the restore list or password prompt.
- **No secrets in the image.** The password is supplied when the container starts,
  never built in, and `.dockerignore` keeps `config.yaml` and `.env` out of the build.
- **The container runs as a non-root user.** Backups are written to `/backups`.
- **The passwords in `docker-compose.yml` are for the local demo only.**
