import logging

from app.config import get_db_backend
from app.db_base import BaseAlertDatabase
from app.db_sqlite_disk import DiskSqliteDatabase
from app.db_sqlite_memory import MemorySqliteDatabase

logger = logging.getLogger("alert_filter.database")


def create_database_instance() -> BaseAlertDatabase:
    backend = get_db_backend()
    if backend == "sqlite_memory":
        logger.debug("Выбран режим базы данных: SQLite в оперативной памяти (sqlite_memory).")
        return MemorySqliteDatabase()
    elif backend == "sqlite_disk":
        logger.debug("Выбран режим базы данных: SQLite на диске (sqlite_disk).")
        return DiskSqliteDatabase()
    else:
        raise ValueError(
            f"Неизвестный тип базы данных в config.json: '{backend}'. "
            "Допустимые значения: 'sqlite_disk' или 'sqlite_memory'."
        )


db: BaseAlertDatabase = create_database_instance()
