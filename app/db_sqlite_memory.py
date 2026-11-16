import logging
import os
import sqlite3
from typing import Optional

from app.config import DB_PATH
from app.db_base import BaseAlertDatabase

logger = logging.getLogger("alert_filter.db_memory")


class MemorySqliteDatabase(BaseAlertDatabase):
    def __init__(self):
        self._mem_conn: Optional[sqlite3.Connection] = None
        self._is_dirty: bool = False

    def init(self):
        try:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys = ON")

            if os.path.exists(DB_PATH) and os.path.getsize(DB_PATH) > 0:
                with sqlite3.connect(DB_PATH) as disk_conn:
                    disk_conn.backup(self._mem_conn)
                logger.info("База данных загружена с диска в оперативную память: %s", DB_PATH)
            else:
                logger.info("Создана новая пустая база данных в оперативной памяти.")

            with self._mem_conn:
                self._create_schema(self._mem_conn)
            self._is_dirty = False
        except Exception as e:
            logger.error("Ошибка инициализации БД SQLite в памяти: %s", e, exc_info=True)
            raise

    def _execute_read(self, callback):
        if self._mem_conn is None:
            raise RuntimeError("База данных в памяти не инициализирована.")
        try:
            with self._mem_conn:
                return callback(self._mem_conn)
        except Exception as e:
            logger.error("Ошибка чтения из БД в памяти: %s", e, exc_info=True)
            raise

    def _execute_write(self, callback):
        if self._mem_conn is None:
            raise RuntimeError("База данных в памяти не инициализирована.")
        try:
            with self._mem_conn:
                result = callback(self._mem_conn)
            self._is_dirty = True
            return result
        except Exception as e:
            logger.error("Ошибка записи в БД в памяти: %s", e, exc_info=True)
            raise

    def flush_to_disk(self, force: bool = False):
        if self._mem_conn is None:
            return
        if not self._is_dirty and not force:
            logger.debug("Сброс памяти на диск пропущен: новых изменений за период не было (диски NAS не тревожим).")
            return

        try:
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
            with sqlite3.connect(DB_PATH) as disk_conn:
                self._mem_conn.backup(disk_conn)
            self._is_dirty = False
            logger.info("Снимок базы данных из оперативной памяти успешно сохранен на диск: %s", DB_PATH)
        except Exception as e:
            logger.error("Ошибка сброса базы данных из памяти на диск: %s", e, exc_info=True)

    def close(self):
        self.flush_to_disk(force=False)
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None
            logger.info("Соединение с базой данных в оперативной памяти закрыто.")
