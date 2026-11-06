import pytest
from typer.testing import CliRunner

from yourtool.cli import app
from yourtool.config import ConfigError, resolve_config

runner = CliRunner()

YAML = """\
database:
  host: filehost
  port: 6000
  user: fileuser
  name: filedb
storage:
  path: ./from-file
"""


@pytest.fixture
def yaml_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(YAML)
    return p


def test_defaults_and_required_from_cli():
    cfg, src = resolve_config(cli={"db_user": "u", "db_name": "d"}, env={})
    assert cfg.db_host == "localhost"
    assert cfg.db_port == 5432
    assert src["db_host"] == "default"
    assert src["db_user"] == "cli"


def test_yaml_values_used(yaml_file):
    cfg, src = resolve_config(config_file=yaml_file, env={})
    assert cfg.db_host == "filehost"
    assert cfg.db_port == 6000
    assert src["db_host"] == "file"


def test_env_overrides_yaml(yaml_file):
    cfg, src = resolve_config(
        config_file=yaml_file, env={"YOURTOOL_DB_HOST": "envhost"}
    )
    assert cfg.db_host == "envhost"
    assert src["db_host"] == "env"
    assert cfg.db_user == "fileuser"


def test_cli_overrides_env_and_yaml(yaml_file):
    cfg, src = resolve_config(
        cli={"db_host": "clihost"},
        config_file=yaml_file,
        env={"YOURTOOL_DB_HOST": "envhost"},
    )
    assert cfg.db_host == "clihost"
    assert src["db_host"] == "cli"


def test_password_from_env():
    cfg, _ = resolve_config(
        cli={"db_user": "u", "db_name": "d"},
        env={"YOURTOOL_DB_PASSWORD": "s3cret"},
    )
    assert cfg.db_password == "s3cret"


def test_missing_required():
    with pytest.raises(ConfigError, match="db_user"):
        resolve_config(env={})


def test_bad_port():
    with pytest.raises(ConfigError, match="db_port"):
        resolve_config(cli={"db_user": "u", "db_name": "d", "db_port": 99999}, env={})


def test_unsupported_db_type():
    with pytest.raises(ConfigError, match="Unsupported db_type"):
        resolve_config(cli={"db_user": "u", "db_name": "d", "db_type": "oracle"}, env={})


def test_unknown_yaml_key_is_caught(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("database:\n  hots: typo\n")
    with pytest.raises(ConfigError, match="Unknown key 'hots'"):
        resolve_config(config_file=p, env={})


def test_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        resolve_config(config_file=__import__("pathlib").Path("nope.yaml"), env={})


def test_cli_config_show_masks_password():
    result = runner.invoke(
        app,
        ["config", "show", "--db-user", "u", "--db-name", "d"],
        env={"YOURTOOL_DB_PASSWORD": "s3cret"},
    )
    assert result.exit_code == 0
    assert "s3cret" not in result.stdout
    assert "********" in result.stdout


def test_cli_config_show_error_exit_code():
    result = runner.invoke(app, ["config", "show"], env={})
    assert result.exit_code == 1


# ---- log file setting (Step 7) ----


def test_log_file_defaults_to_none():
    cfg, _ = resolve_config(cli={"db_user": "u", "db_name": "d"}, env={})
    assert cfg.log_file is None


def test_log_file_from_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("database:\n  user: u\n  name: d\nlogging:\n  file: ./x.log\n")
    cfg, src = resolve_config(config_file=p, env={})
    assert cfg.log_file == "./x.log" and src["log_file"] == "file"


def test_log_file_env_overrides_yaml_and_cli_overrides_env(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("database:\n  user: u\n  name: d\nlogging:\n  file: ./yaml.log\n")
    cfg, _ = resolve_config(config_file=p, env={"YOURTOOL_LOG_FILE": "env.log"})
    assert cfg.log_file == "env.log"
    cfg, src = resolve_config(
        cli={"log_file": "cli.log"}, config_file=p, env={"YOURTOOL_LOG_FILE": "env.log"}
    )
    assert cfg.log_file == "cli.log" and src["log_file"] == "cli"


def test_unknown_logging_key_is_caught(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("database:\n  user: u\n  name: d\nlogging:\n  flie: typo.log\n")
    with pytest.raises(ConfigError, match="Unknown key 'flie'"):
        resolve_config(config_file=p, env={})


# ---- default port depends on the database type (Step 9) ----


def test_postgres_default_port():
    cfg, src = resolve_config(cli={"db_user": "u", "db_name": "d"}, env={})
    assert cfg.db_port == 5432 and src["db_port"] == "default"


def test_mysql_default_port():
    cfg, src = resolve_config(cli={"db_user": "u", "db_name": "d", "db_type": "mysql"}, env={})
    assert cfg.db_port == 3306 and src["db_port"] == "default"


def test_explicit_port_beats_the_default():
    cfg, src = resolve_config(
        cli={"db_user": "u", "db_name": "d", "db_type": "mysql", "db_port": 3307}, env={}
    )
    assert cfg.db_port == 3307 and src["db_port"] == "cli"


def test_mysql_type_from_environment():
    cfg, _ = resolve_config(
        cli={"db_user": "u", "db_name": "d"}, env={"YOURTOOL_DB_TYPE": "mysql"}
    )
    assert cfg.db_type == "mysql" and cfg.db_port == 3306


# ---- cloud storage settings (Step 10) ----


def test_s3_settings_default_to_unset():
    cfg, _ = resolve_config(cli={"db_user": "u", "db_name": "d"}, env={})
    assert cfg.storage_type == "local"
    assert (cfg.storage_bucket, cfg.storage_prefix, cfg.storage_region, cfg.storage_endpoint_url) == (
        None, None, None, None,
    )


def test_s3_requires_a_bucket():
    with pytest.raises(ConfigError, match="storage_bucket is required"):
        resolve_config(cli={"db_user": "u", "db_name": "d", "storage_type": "s3"}, env={})


def test_s3_settings_from_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "database:\n  user: u\n  name: d\n"
        "storage:\n  type: s3\n  bucket: my-bucket\n  prefix: backups/prod\n"
        "  region: eu-west-1\n  endpoint_url: http://localhost:9000\n"
    )
    cfg, src = resolve_config(config_file=p, env={})
    assert (cfg.storage_type, cfg.storage_bucket, cfg.storage_prefix) == ("s3", "my-bucket", "backups/prod")
    assert (cfg.storage_region, cfg.storage_endpoint_url) == ("eu-west-1", "http://localhost:9000")
    assert src["storage_bucket"] == "file"


def test_s3_settings_from_environment_and_flags_override():
    env = {
        "YOURTOOL_STORAGE_TYPE": "s3",
        "YOURTOOL_STORAGE_BUCKET": "env-bucket",
        "YOURTOOL_STORAGE_PREFIX": "env/prefix",
        "YOURTOOL_STORAGE_REGION": "us-west-2",
    }
    cfg, src = resolve_config(cli={"db_user": "u", "db_name": "d"}, env=env)
    assert cfg.storage_bucket == "env-bucket" and src["storage_bucket"] == "env"
    cfg, src = resolve_config(
        cli={"db_user": "u", "db_name": "d", "storage_bucket": "flag-bucket"}, env=env
    )
    assert cfg.storage_bucket == "flag-bucket" and src["storage_bucket"] == "cli"


@pytest.mark.parametrize("bad", ["localhost:9000", "ftp://x", "minio"])
def test_endpoint_url_must_be_http_or_https(bad):
    with pytest.raises(ConfigError, match="must start with http"):
        resolve_config(
            cli={"db_user": "u", "db_name": "d", "storage_type": "s3",
                 "storage_bucket": "b", "storage_endpoint_url": bad},
            env={},
        )


def test_database_settings_not_required_when_the_caller_says_so():
    cfg, _ = resolve_config(cli={"storage_type": "local"}, env={}, required=())
    assert cfg.db_user == "" and cfg.db_name == ""


def test_unknown_storage_yaml_key_still_caught(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("database:\n  user: u\n  name: d\nstorage:\n  bucekt: typo\n")
    with pytest.raises(ConfigError, match="Unknown key 'bucekt'"):
        resolve_config(config_file=p, env={})
