from yourtool.config import Config
from yourtool.databases.base import DatabaseAdapter, DatabaseError
from yourtool.databases.mysql import MySqlAdapter
from yourtool.databases.postgres import PostgresAdapter

# To support a new database, write an adapter and add it here.
_ADAPTERS: dict[str, type[DatabaseAdapter]] = {
    "postgres": PostgresAdapter,
    "mysql": MySqlAdapter,
}


def get_adapter_class(db_type: str) -> type[DatabaseAdapter]:
    try:
        return _ADAPTERS[db_type]
    except KeyError:
        raise DatabaseError(f"No adapter for database type '{db_type}'.") from None


def get_adapter(config: Config) -> DatabaseAdapter:
    return get_adapter_class(config.db_type)(config)


__all__ = [
    "DatabaseAdapter",
    "DatabaseError",
    "MySqlAdapter",
    "PostgresAdapter",
    "get_adapter",
    "get_adapter_class",
]
