"""Реализация дискового хранилища алертов на базе SQLite."""

import logging
import os
import sqlite3

from app.config import DB_PATH
from app.db_base import BaseAlertDatabase

logger = logging.getLogger("alert_filter.db_disk")


class DiskSqliteDatabase(BaseAlertDatabase):
    """Хранилище связок и событий в файле SQLite на диске в режиме WAL."""

    def _get_conn(self) -> sqlite3.Connection:
        """Открывает новое соединение с файлом SQLite и включает внешние ключи."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        """Инициализирует файл базы данных на диске и создает таблицы схемы."""
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
        """Выполняет операцию чтения в контексте дискового соединения."""
        try:
            with self._get_conn() as conn:
                return callback(conn)
        except Exception as e:
            logger.error("Ошибка чтения из дисковой БД SQLite: %s", e, exc_info=True)
            raise

    def _execute_write(self, callback):
        """Выполняет транзакционную операцию записи в дисковую БД."""
        try:
            with self._get_conn() as conn:
                return callback(conn)
        except Exception as e:
            logger.error("Ошибка записи в дисковую БД SQLite: %s", e, exc_info=True)
            raise

    def flush_to_disk(self, force: bool = False) -> None:
        """Пустая операция: в дисковом режиме изменения фиксируются сразу."""
        pass

    def close(self) -> None:
        """Завершает работу с дисковой базой данных."""
        logger.debug("Дисковая БД SQLite завершила работу.")
