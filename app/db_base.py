"""Базовый абстрактный класс хранилища алертов и SQL-логика работы со связками."""

import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.config import get_retention_days
from app.utils import is_substring_pattern, make_bundle_hash, match_message

logger = logging.getLogger("alert_filter.db_base")


class BaseAlertDatabase(ABC):
    """Абстрактный базовый класс для реализаций хранилища связок и событий SQLite."""

    @abstractmethod
    def init(self) -> None:
        """Инициализирует подключение к базе данных и схему таблиц."""
        pass

    @abstractmethod
    def flush_to_disk(self, force: bool = False) -> None:
        """Принудительно или по расписанию сохраняет данные на диск."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Корректно закрывает соединение с базой данных."""
        pass

    @abstractmethod
    def _execute_read(self, callback):
        """Выполняет функцию чтения `callback(conn)` в безопасном контексте."""
        pass

    @abstractmethod
    def _execute_write(self, callback):
        """Выполняет функцию записи `callback(conn)` в безопасном контексте."""
        pass

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Создает таблицы `bundles`, `events` и индексы, если они не существуют."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bundles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bundle_hash TEXT UNIQUE NOT NULL,
                host TEXT NOT NULL,
                service TEXT NOT NULL DEFAULT 'unknown',
                program TEXT NOT NULL,
                message TEXT NOT NULL,
                description TEXT DEFAULT '',
                last_triggered TEXT NOT NULL,
                is_blocked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bundle_id INTEGER NOT NULL,
                triggered_at TEXT NOT NULL,
                FOREIGN KEY(bundle_id) REFERENCES bundles(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_bundles_hsp ON bundles(host, service, program);
            CREATE INDEX IF NOT EXISTS idx_bundles_last ON bundles(last_triggered);
            CREATE INDEX IF NOT EXISTS idx_events_bundle ON events(bundle_id);
            CREATE INDEX IF NOT EXISTS idx_events_time ON events(triggered_at);
        """)

    def _cleanup_old_records(self, conn: sqlite3.Connection) -> bool:
        """Удаляет устаревшие события и неактивные связки старше `retention_days`."""
        days = get_retention_days()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        cur_events = conn.execute("DELETE FROM events WHERE triggered_at < ?", (cutoff,))
        if cur_events.rowcount > 0:
            logger.debug("Удалено устаревших записей истории сработок: %d", cur_events.rowcount)

        cur_bundles = conn.execute("DELETE FROM bundles WHERE last_triggered < ?", (cutoff,))
        if cur_bundles.rowcount > 0:
            logger.info("Удалено неактивных связок старше %d дн.: %d", days, cur_bundles.rowcount)

        return (cur_events.rowcount + cur_bundles.rowcount) > 0

    def _find_matching_bundle(
        self, conn: sqlite3.Connection, host: str, service: str, program: str, message: str
    ):
        """Ищет существующую связку по точному хешу или по шаблону подстроки `{{...}}`."""
        b_hash = make_bundle_hash(host, service, program, message)
        exact = conn.execute(
            "SELECT * FROM bundles WHERE bundle_hash = ?", (b_hash,)
        ).fetchone()
        if exact:
            logger.debug("Найдено точное совпадение связки в БД (ID=%d)", exact["id"])
            return exact

        candidates = conn.execute(
            "SELECT * FROM bundles WHERE lower(host) = ? AND lower(service) = ? AND lower(program) = ?",
            (host.lower(), service.lower(), program.lower()),
        ).fetchall()

        for row in candidates:
            if is_substring_pattern(row["message"]) and match_message(row["message"], message):
                logger.debug(
                    "Найдено совпадение по шаблону подстроки в БД (ID=%d, шаблон=%r)",
                    row["id"],
                    row["message"],
                )
                return row

        return None

    def process_incoming_event(self, clean_data: dict) -> Optional[dict]:
        """Регистрирует входящее событие и возвращает данные для отправки, если связка не заблокирована."""
        now_iso = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection):
            self._cleanup_old_records(conn)

            row = self._find_matching_bundle(
                conn,
                clean_data["host"],
                clean_data["service"],
                clean_data["program"],
                clean_data["message"],
            )

            if row:
                bundle_id = row["id"]
                is_blocked = bool(row["is_blocked"])
                description = row["description"]
                conn.execute(
                    "UPDATE bundles SET last_triggered = ? WHERE id = ?",
                    (now_iso, bundle_id),
                )
                logger.debug(
                    "Обновлено время последней сработки для связки ID=%d (is_blocked=%s)",
                    bundle_id,
                    is_blocked,
                )
            else:
                cur = conn.execute(
                    "INSERT INTO bundles (bundle_hash, host, service, program, message, description, last_triggered) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        clean_data["bundle_hash"],
                        clean_data["host"],
                        clean_data["service"],
                        clean_data["program"],
                        clean_data["message"],
                        clean_data["message"],
                        now_iso,
                    ),
                )
                bundle_id = cur.lastrowid
                is_blocked = False
                description = clean_data["message"]
                logger.info(
                    "Новая связка добавлена в базу данных (ID=%d, host=%s, service=%s, program=%s)",
                    bundle_id,
                    clean_data["host"],
                    clean_data["service"],
                    clean_data["program"],
                )

            conn.execute(
                "INSERT INTO events (bundle_id, triggered_at) VALUES (?, ?)",
                (bundle_id, now_iso),
            )

            if is_blocked:
                logger.debug("Связка ID=%d заблокирована, уведомление не требуется.", bundle_id)
                return None

            return {
                "id": bundle_id,
                "host": clean_data["host"],
                "service": clean_data["service"],
                "program": clean_data["program"],
                "message": clean_data["message"],
                "description": description,
            }

        return self._execute_write(_op)

    def create_manual_bundle(
        self,
        host: str,
        service: str,
        program: str,
        message: str,
        description: str,
        is_blocked: bool,
    ) -> int:
        """Создает новую связку вручную через веб-интерфейс и возвращает ее ID."""
        b_hash = make_bundle_hash(host, service, program, message)
        now_iso = datetime.now(timezone.utc).isoformat()
        blocked_val = 1 if is_blocked else 0
        desc_val = description.strip() if description.strip() else message

        def _op(conn: sqlite3.Connection):
            cur = conn.execute(
                "INSERT INTO bundles (bundle_hash, host, service, program, message, description, last_triggered, is_blocked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (b_hash, host, service, program, message, desc_val, now_iso, blocked_val),
            )
            bundle_id = cur.lastrowid
            logger.info("Вручную создана новая связка в базе (ID=%d)", bundle_id)
            return bundle_id

        return self._execute_write(_op)

    def update_full_bundle(
        self,
        bundle_id: int,
        host: str,
        service: str,
        program: str,
        message: str,
        description: str,
        is_blocked: bool,
    ) -> None:
        """Полностью обновляет поля существующей связки по ее ID."""
        b_hash = make_bundle_hash(host, service, program, message)
        blocked_val = 1 if is_blocked else 0

        def _op(conn: sqlite3.Connection):
            conn.execute(
                """
                UPDATE bundles
                SET bundle_hash = ?, host = ?, service = ?, program = ?, message = ?, description = ?, is_blocked = ?
                WHERE id = ?
                """,
                (b_hash, host, service, program, message, description.strip(), blocked_val, bundle_id),
            )
            logger.debug("Связка ID=%d полностью обновлена через веб-интерфейс.", bundle_id)

        self._execute_write(_op)

    def get_or_create_and_block_bundle(
        self, host: str, service: str, program: str, message: str
    ) -> int:
        """Находит связку (или создает новую) и переводит ее в статус заблокированной."""
        now_iso = datetime.now(timezone.utc).isoformat()

        def _op(conn: sqlite3.Connection):
            row = self._find_matching_bundle(conn, host, service, program, message)
            if row:
                bundle_id = row["id"]
                conn.execute("UPDATE bundles SET is_blocked = 1 WHERE id = ?", (bundle_id,))
                logger.debug("Существующая связка ID=%d заблокирована по API.", bundle_id)
            else:
                b_hash = make_bundle_hash(host, service, program, message)
                cur = conn.execute(
                    "INSERT INTO bundles (bundle_hash, host, service, program, message, description, last_triggered, is_blocked) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (b_hash, host, service, program, message, message, now_iso),
                )
                bundle_id = cur.lastrowid
                logger.info("Новая связка создана и заблокирована по API (ID=%d).", bundle_id)
            return bundle_id

        return self._execute_write(_op)

    def fetch_filter_options(self) -> dict:
        """Возвращает списки уникальных хостов, unit и container для выпадающих фильтров."""
        def _op(conn: sqlite3.Connection):
            hosts = [
                r["host"]
                for r in conn.execute("SELECT DISTINCT host FROM bundles ORDER BY host ASC").fetchall()
            ]
            units = [
                r["program"]
                for r in conn.execute(
                    "SELECT DISTINCT program FROM bundles WHERE service = 'unit' ORDER BY program ASC"
                ).fetchall()
            ]
            containers = [
                r["program"]
                for r in conn.execute(
                    "SELECT DISTINCT program FROM bundles WHERE service = 'container' ORDER BY program ASC"
                ).fetchall()
            ]
            return {"hosts": hosts, "units": units, "containers": containers}

        return self._execute_read(_op)

    def fetch_bundles(
        self,
        only_blocked: bool,
        since_delta: Optional[timedelta],
        filter_host: str = "",
        filter_unit: str = "",
        filter_container: str = "",
        search_msg: str = "",
        page: int = 1,
        page_size: int = 25,
    ) -> dict:
        """Выполняет фильтрацию, подсчет сработок и постраничную выборку связок."""
        where_clauses = ["1=1"]
        params = []

        if only_blocked:
            where_clauses.append("b.is_blocked = 1")

        if since_delta is not None:
            cutoff = (datetime.now(timezone.utc) - since_delta).isoformat()
            where_clauses.append("b.last_triggered >= ?")
            params.append(cutoff)

        if filter_host:
            where_clauses.append("b.host = ?")
            params.append(filter_host)

        if filter_unit and filter_container:
            where_clauses.append(
                "((b.service = 'unit' AND b.program = ?) OR (b.service = 'container' AND b.program = ?))"
            )
            params.extend([filter_unit, filter_container])
        elif filter_unit:
            where_clauses.append("b.service = 'unit' AND b.program = ?")
            params.append(filter_unit)
        elif filter_container:
            where_clauses.append("b.service = 'container' AND b.program = ?")
            params.append(filter_container)

        if search_msg:
            where_clauses.append("lower(b.message) LIKE ?")
            params.append(f"%{search_msg.lower()}%")

        where_sql = " AND ".join(where_clauses)
        safe_page_size = max(1, int(page_size))

        def _op(conn: sqlite3.Connection):
            self._cleanup_old_records(conn)

            count_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM bundles b WHERE {where_sql}",
                params,
            ).fetchone()
            total_count = int(count_row["cnt"]) if count_row else 0
            total_pages = max(1, (total_count + safe_page_size - 1) // safe_page_size)
            current_page = min(max(1, int(page)), total_pages)
            offset = (current_page - 1) * safe_page_size

            data_query = f"""
                SELECT b.*, COUNT(e.id) as trigger_count
                FROM bundles b
                LEFT JOIN events e ON b.id = e.bundle_id
                WHERE {where_sql}
                GROUP BY b.id
                ORDER BY b.last_triggered DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(data_query, [*params, safe_page_size, offset]).fetchall()
            return {
                "items": [dict(r) for r in rows],
                "total_count": total_count,
                "total_pages": total_pages,
                "current_page": current_page,
            }

        return self._execute_read(_op)

    def fetch_bundle_by_id(self, bundle_id: int) -> Optional[dict]:
        """Возвращает подробную информацию о связке по ID вместе со счетчиком сработок."""
        def _op(conn: sqlite3.Connection):
            self._cleanup_old_records(conn)
            row = conn.execute(
                """
                SELECT b.*, COUNT(e.id) as trigger_count
                FROM bundles b
                LEFT JOIN events e ON b.id = e.bundle_id
                WHERE b.id = ?
                GROUP BY b.id
                """,
                (bundle_id,),
            ).fetchone()
            return dict(row) if row else None

        return self._execute_read(_op)

    def set_bundles_block_status(self, bundle_ids: List[int], is_blocked: bool) -> None:
        """Массово устанавливает флаг блокировки для списка ID связок."""
        if not bundle_ids:
            return
        placeholders = ",".join("?" for _ in bundle_ids)
        val = 1 if is_blocked else 0

        def _op(conn: sqlite3.Connection):
            conn.execute(
                f"UPDATE bundles SET is_blocked = {val} WHERE id IN ({placeholders})",
                bundle_ids,
            )
            logger.debug("Массовое изменение статуса блокировки (%s) для ID: %s", is_blocked, bundle_ids)

        self._execute_write(_op)

    def delete_bundles(self, bundle_ids: List[int]) -> None:
        """Массово удаляет связки (и связанные события по CASCADE) по списку ID."""
        if not bundle_ids:
            return
        placeholders = ",".join("?" for _ in bundle_ids)

        def _op(conn: sqlite3.Connection):
            conn.execute(f"DELETE FROM bundles WHERE id IN ({placeholders})", bundle_ids)
            logger.debug("Удалены связки из БД с ID: %s", bundle_ids)

        self._execute_write(_op)
