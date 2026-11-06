# How yourtool works

A guided tour of the project: what each part does, why it was built that way, and what to say about it.

## 1. The big picture

```
   you type a command            settings come from, in priority order:
          |                        flags > environment variables > config file > defaults
   cli.py (Typer)  ------------>  config.py
          |
   workflows (plain Python, no command-line code)
   backup.py  restore.py  retention.py  integrity.py  jobs.py  scheduler.py
          |
   +------+-------------------+
   |                          |
 database adapters        storage adapters
 postgres.py  mysql.py    local.py   s3.py
 (run pg_dump, mysqldump) (a folder / a bucket)
```

The key idea is the **adapter pattern**. The workflows only know two small interfaces ("a database can back up and
restore", "a storage place can upload, list, download, delete"). Adding MySQL, or S3, meant writing one new
file and registering it. The command line and the workflows did not change.

## 2. Life of a backup

1. The CLI resolves settings and asks for the password if needed (hidden typing).
2. `pg_dump` (or `mysqldump`) writes a dump into a **private temporary folder**.
3. Optionally the dump is gzip-compressed (streamed in 1 MB pieces, reproducible bytes).
4. A SHA-256 checksum of the stored file is computed.
5. The backup is uploaded, **then** its `.sha256` file (so a checksum file implies a complete backup).
6. The temporary folder is deleted whatever happens.

## 3. Life of a restore (nothing changes until the last step)

download, **verify the checksum**, decompress, ask the database tool whether the file is valid, look at the target
database, **show a summary and ask for confirmation**, then create the database (if new) and restore.
PostgreSQL restores run as one transaction, so a failure leaves the database unchanged. MySQL cannot roll back
table creation, so it refuses to restore into a non-empty database without `--clean` and removes a new database it
created if the restore fails.

## 4. The modules

| Module | Job |
|---|---|
| `cli.py` | Commands and flags; turns every failure into a clean message and exit code |
| `config.py` | Layered settings and validation |
| `backup.py`, `restore.py` | The two main workflows |
| `integrity.py`, `compression.py` | Checksums and gzip |
| `retention.py` | Which backups to keep (`plan_retention` decides, `execute_prune` deletes) |
| `cron.py`, `schedule.py`, `scheduler.py`, `jobs.py` | Cron parser, schedule file, the loop, the job runners |
| `databases/`, `storage/` | The adapters |
| `utils/logsetup.py` | Terminal and file logging, with secrets scrubbed |

## 5. The technologies, in plain terms

- **Python packaging (`pyproject.toml`, virtual environment, `pip install -e`)**: declares the project and makes `yourtool` a real command.
- **Typer**: builds the command line from function signatures.
- **YAML + environment variables**: configuration without hard-coding secrets.
- **`subprocess`**: runs `pg_dump` and friends. Arguments are passed as a list, never one string, so a strange database name cannot inject commands.
- **boto3 / S3 / IAM**: cloud storage. Credentials come from the standard AWS chain, never from our config.
- **SHA-256 and gzip**: integrity and size. A checksum catches accidental damage, not deliberate tampering.
- **Docker and Compose**: a multi-stage, non-root image, plus a one-command demo with PostgreSQL, MariaDB and an S3 mock.
- **pytest**: unit tests with fakes, integration tests against real servers, and randomized property tests.
- **GitHub Actions**: runs the tests and the Docker demo on each push (configured; needs a GitHub repository to run).
- **Cron and time zones**: a hand-written cron parser, daylight-saving aware, with times kept in UTC internally.

## 6. Decisions worth explaining

- **Passwords**: environment variable or hidden prompt, never a flag. MySQL gets it through a private temporary file. A filter scrubs known secrets from every log line.
- **Safety by confirmation**: restore into an existing database needs the database name typed; `prune` needs the word `delete`; both have dry-run or preview first.
- **The newest backup is never deleted**, whatever the retention rules say (checked by randomized tests).
- **Scheduled deletion is opt-in**: a prune job will not even load without `delete_without_asking: true`.
- **UTC everywhere** in file names, logs and inside the scheduler, so daylight saving cannot reorder things.
- **Decide, then do**: `plan_retention` is a pure function, so a plan can be shown, tested and trusted before anything is deleted.

## 7. Bugs found along the way (good stories)

- File names turned `college_grades` into `college-grades`, because I over-sanitized. Found by a test.
- A cleanup step could raise a second error that hid the real one. Found by an error-path test.
- A test server bound to `0.0.0.0`, which Windows cannot connect to. Found by running on Windows.
- Windows turned `\n` into `\r\n` in a test file. Found on Windows.
- Re-configuring the logger wiped registered secrets, so the scheduler printed a password. Found by a test.
- Python compares same-zone times by wall clock, which mis-orders the repeated hour in autumn. Fixed by using UTC internally.

The pattern: write the tests, break the code on purpose to prove the tests notice, and run on the real platform.

## 8. Honest limits

- Real Amazon S3 was not tested, only a mock S3 server. GitHub Actions and the Docker demo were configured and checked, but not run by the assistant that built them; run them yourself before claiming them.
- PostgreSQL's backup format is already compressed, so gzip adds little there.
- The scheduler needs its process to be running; there is no clustering or lock, so run one per storage location.
- Checksums do not stop deliberate tampering; encryption and signing are not implemented.
- Notifications (Slack, email) and multiple-database backups in one command are not implemented.

## 9. Questions to be ready for

1. *Why an adapter pattern?* Two interfaces mean a new database or storage backend is one new file.
2. *How do you keep a restore from destroying data?* Verify first, typed confirmation, single transaction where possible.
3. *How did you test it?* Fakes for speed, real servers for truth, random inputs for safety rules, and deliberately broken code to prove the tests can fail.
4. *What was the hardest bug?* Pick the daylight-saving or the secret-scrubbing story.
5. *What would you do next?* Notifications, encryption, a real S3 test, and a lock to prevent two schedulers.
