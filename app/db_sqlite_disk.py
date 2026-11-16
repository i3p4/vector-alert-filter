import logging
import os
import sqlite3

from app.config import DB_PATH
from app.db_base import BaseAlertDatabase

logger = logging.getLogger("alert_filter.db_disk")


class DiskSqliteDatabase(BaseAlertDatabase):
    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self):
        try:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            with self._get_conn() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                self._create_schema(conn)
            logger.info("Инициализирована база данных SQLite на диске: %s", DB_PATH)
        except Exception as e:
            logger.error("Ошибка инициализации дисковой БД SQLite: %s", e, exc_info=True)
            raise

    def _execute_read(self, callback):
        try:
            with self._get_conn() as conn:
                return callback(conn)
        except Exception as e:
            logger.error("Ошибка чтения из дисковой БД SQLite: %s", e, exc_info=True)
            raise

    def _execute_write(self, callback):
        try:
            with self._get_conn() as conn:
                return callback(conn)
        except Exception as e:
            logger.error("Ошибка записи в дисковую БД SQLite: %s", e, exc_info=True)
            raise

    def flush_to_disk(self, force: bool = False):
        # В дисковом режиме все изменения уже записываются сразу на диск
        pass

    def close(self):
        logger.debug("Дисковая БД SQLite завершила работу.")
