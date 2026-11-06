"""Configuration loading.

Settings can come from four places. Highest priority wins:

    1. CLI flags
    2. Environment variables  (YOURTOOL_DB_HOST, YOURTOOL_DB_PASSWORD, ...)
    3. YAML config file
    4. Built-in defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

ENV_PREFIX = "YOURTOOL_"

# "mysql" covers both MySQL and MariaDB (they use the same client tools).
SUPPORTED_DB_TYPES = {"postgres", "mysql"}

# Used when no port is given; depends on the database type.
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}
SUPPORTED_COMPRESSION = {"none", "gzip"}

RETENTION_KEYS = ("keep_last", "keep_daily", "keep_weekly", "keep_monthly")

# "s3" covers Amazon S3 and any service that speaks the same protocol.
SUPPORTED_STORAGE_TYPES = {"local", "s3"}

DEFAULTS: dict[str, Any] = {
    "db_type": "postgres",
    "db_host": "localhost",
    "storage_type": "local",
    "storage_path": "./backups",
    "compression": "none",
}

REQUIRED = ("db_user", "db_name")

# YAML is nested for readability; internally everything is flat.
YAML_SECTIONS = {
    "database": ("db", {"type", "host", "port", "user", "password", "name"}),
    "storage": ("storage", {"type", "path", "bucket", "prefix", "region", "endpoint_url"}),
    "logging": ("log", {"file"}),
    "backup": ("", {"compression"}),
    "retention": ("", {"keep_last", "keep_daily", "keep_weekly", "keep_monthly"}),
}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    db_type: str
    db_host: str
    db_port: int
    # Defaults below let commands that do not touch a database (like `list`) work
    # without database settings. Commands that need them check REQUIRED.
    db_user: str = ""
    db_name: str = ""
    storage_type: str = "local"
    storage_path: str = "./backups"  # local storage: the backup folder
    compression: str = "none"  # "none" or "gzip"
    db_password: str | None = None
    log_file: str | None = None  # None = no log file
    # Cloud storage (storage_type "s3"); ignored for local storage.
    storage_bucket: str | None = None
    storage_prefix: str | None = None  # a "folder" inside the bucket
    storage_region: str | None = None
    storage_endpoint_url: str | None = None  # for S3-compatible services
    # Retention rules for `prune` (None = rule not set). See yourtool/retention.py.
    keep_last: int | None = None
    keep_daily: int | None = None
    keep_weekly: int | None = None
    keep_monthly: int | None = None


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {path}: {exc}") from None

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from None

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level.")

    flat: dict[str, Any] = {}
    for section, values in data.items():
        if section == "schedule":
            continue  # read by yourtool.schedule
        if section not in YAML_SECTIONS:
            raise ConfigError(
                f"Unknown section '{section}' in {path}. "
                f"Expected: {', '.join(YAML_SECTIONS)}."
            )
        prefix, allowed = YAML_SECTIONS[section]
        if not isinstance(values, dict):
            raise ConfigError(f"Section '{section}' in {path} must be a mapping.")
        for key, value in values.items():
            if key not in allowed:
                raise ConfigError(
                    f"Unknown key '{key}' in section '{section}' of {path}. "
                    f"Allowed: {', '.join(sorted(allowed))}."
                )
            if value is not None:
                flat[f"{prefix}_{key}" if prefix else key] = value
    return flat


def _load_env(env: Mapping[str, str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for f in fields(Config):
        value = env.get(ENV_PREFIX + f.name.upper())
        if value:  # ignore unset and empty
            found[f.name] = value
    return found


def resolve_config(
    cli: Mapping[str, Any] | None = None,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    required: tuple[str, ...] = REQUIRED,
) -> tuple[Config, dict[str, str]]:
    """Merge all sources and validate.

    Returns the final Config plus a dict saying where each value came from
    (useful for debugging with `yourtool config show`).
    """
    env = os.environ if env is None else env

    layers: list[tuple[str, dict[str, Any]]] = [("default", dict(DEFAULTS))]
    if config_file is not None:
        layers.append(("file", _load_yaml(config_file)))
    layers.append(("env", _load_env(env)))
    layers.append(("cli", {k: v for k, v in (cli or {}).items() if v is not None}))

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for source, layer in layers:
        for key, value in layer.items():
            values[key] = value
            sources[key] = source

    missing = [k for k in required if not values.get(k)]
    if missing:
        names = ", ".join(missing)
        raise ConfigError(
            f"Missing required setting(s): {names}. "
            "Provide them via flags, environment variables, or the config file."
        )

    if values["db_type"] not in SUPPORTED_DB_TYPES:
        raise ConfigError(
            f"Unsupported db_type '{values['db_type']}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_DB_TYPES))}."
        )

    # No port given anywhere: use the standard one for this kind of database.
    if "db_port" not in values:
        values["db_port"] = DEFAULT_PORTS[values["db_type"]]
        sources["db_port"] = "default"

    try:
        values["db_port"] = int(values["db_port"])
    except (TypeError, ValueError):
        raise ConfigError(f"db_port must be a number, got '{values['db_port']}'.") from None
    if not 1 <= values["db_port"] <= 65535:
        raise ConfigError(f"db_port must be between 1 and 65535, got {values['db_port']}.")

    if values["storage_type"] not in SUPPORTED_STORAGE_TYPES:
        raise ConfigError(
            f"Unsupported storage_type '{values['storage_type']}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_STORAGE_TYPES))}."
        )

    values["compression"] = str(values["compression"]).lower()
    if values["compression"] not in SUPPORTED_COMPRESSION:
        raise ConfigError(
            f"Unsupported compression '{values['compression']}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_COMPRESSION))}."
        )

    for key in RETENTION_KEYS:
        if key not in values:
            continue
        raw = values[key]
        try:
            number = int(raw)
            if isinstance(raw, bool) or number < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be a whole number of 1 or more, got '{raw}'.") from None
        values[key] = number

    if values["storage_type"] == "s3" and not values.get("storage_bucket"):
        raise ConfigError(
            "storage_bucket is required when the storage type is 's3'. Set it with "
            "--bucket, YOURTOOL_STORAGE_BUCKET, or 'bucket:' under 'storage:' in the config file."
        )
    endpoint = values.get("storage_endpoint_url")
    if endpoint and not str(endpoint).startswith(("http://", "https://")):
        raise ConfigError(
            f"storage_endpoint_url must start with http:// or https://, got '{endpoint}'."
        )

    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in values.items() if k in known})
    return config, {k: v for k, v in sources.items() if k in known}
