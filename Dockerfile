# syntax=docker/dockerfile:1

# Which Python to use. Override with: docker build --build-arg PYTHON_VERSION=3.13 .
ARG PYTHON_VERSION=3.12


# ---------------------------------------------------------------------------
# Stage 1 "build": turn the project (and its dependencies) into wheel files.
# Nothing from this stage except the finished wheels reaches the final image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
# ".[s3]" also builds boto3 (the AWS library) so the image can store backups in S3.
# --default-timeout and --retries make the downloads patient on slow or flaky networks
# (pip's defaults are 15 seconds and 5 tries).
RUN pip wheel --no-cache-dir --default-timeout=100 --retries=10 --wheel-dir /wheels ".[s3]"


# ---------------------------------------------------------------------------
# Stage 2 "runtime": the image people actually run.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="yourtool" \
      org.opencontainers.image.description="Command-line database backup and restore tool"

# The database tools the program runs:
#   * PostgreSQL client (pg_dump, pg_restore, psql), version PG_MAJOR below
#   * MySQL-compatible client (mysqldump, mysql). Debian's "default-mysql-client"
#     is MariaDB's client, which works with both MariaDB and MySQL servers.
#
# pg_dump must be the same version as the database server, or newer.
# Debian's own package may be older than your server, so we install the client
# from PostgreSQL's official repository. Match this to your server's version:
#   docker build --build-arg PG_MAJOR=17 .
ARG PG_MAJOR=16

# Package versions are not pinned individually: exact apt versions disappear from
# the repository over time. The version that matters, PG_MAJOR, IS pinned above.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl --fail --silent --show-error --location \
      --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}" default-mysql-client \
 && rm -rf /var/lib/apt/lists/*

# Install the tool from the wheels built in stage 1 (no internet, no build tools).
COPY --from=build /wheels /wheels
# Installs only from the local wheels built from this exact source, never the internet.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir --no-index --find-links=/wheels "yourtool[s3]" \
 && rm -rf /wheels

# Do not run as root. Backups are written to /backups.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir /backups \
 && chown appuser:appuser /backups

ENV YOURTOOL_STORAGE_PATH=/backups \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER 10001
WORKDIR /home/appuser
VOLUME ["/backups"]

# `docker run yourtool backup ...` runs `yourtool backup ...`
ENTRYPOINT ["yourtool"]
CMD ["--help"]
