"""Фоновый рабочий поток обработки событий Vector и запросов веб-интерфейса.

Обеспечивает потокобезопасную последовательную работу с базой данных через
очередь задач `bg_queue` и передачу уведомлений в очередь почтовика `mail_queue`.
"""

import logging
import queue
import threading
import time
from typing import Any, Optional

from app.config import get_memory_flush_seconds
from app.database import db
from app.utils import clean_text_pipeline, extract_clean_vector_event, is_substring_pattern

logger = logging.getLogger("alert_filter.worker")

bg_queue: queue.Queue = queue.Queue()
mail_queue: queue.Queue = queue.Queue()

_bg_thread: Optional[threading.Thread] = None


def submit_vector_payload(payload: Any) -> None:
    """Помещает входящий пакет данных от Vector в очередь фоновой обработки."""
    try:
        bg_queue.put({"source": "vector", "payload": payload})
    except queue.ShutDown:
        logger.debug("Очередь бэкграунда закрывается, новый пакет Vector отклонен.")


def submit_ui_task(action: str, **kwargs) -> Any:
    """Синхронно отправляет задачу веб-интерфейса в фоновый поток БД и ждет ответ."""
    reply_q: queue.Queue = queue.Queue(maxsize=1)
    try:
        bg_queue.put({
            "source": "ui",
            "action": action,
            "params": kwargs,
            "reply_q": reply_q,
        })
    except queue.ShutDown:
        raise RuntimeError("Сервис находится в процессе остановки.")

    status, result = reply_q.get()
    if status == "error":
        raise result
    return result


def _handle_vector_batch(payload: Any) -> None:
    """Разбирает пакет событий от Vector, записывает в БД и передает новые в почтовик."""
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = [payload]
    else:
        logger.error("Получен неподдерживаемый формат данных от Vector: %s", type(payload))
        return

    logger.info("Бэкграунд приступил к разбору пакета Vector (событий: %d).", len(events))
    logger.debug("Сырые данные пакета из Vector: %s", events)

    alerts_to_send = []
    for event in events:
        if not isinstance(event, dict):
            continue
        clean_data = extract_clean_vector_event(event)
        alert_record = db.process_incoming_event(clean_data)
        if alert_record:
            alerts_to_send.append(alert_record)

    if alerts_to_send:
        logger.debug("Передача %d новых событий в очередь почтовика.", len(alerts_to_send))
        try:
            mail_queue.put(alerts_to_send)
        except queue.ShutDown:
            logger.error("Очередь почтовика уже закрыта, события не переданы в отправку.")
    else:
        logger.info("Уведомление на почту не отправлено: все события из пакета заблокированы.")

def _handle_ui_task(action: str, params: dict) -> Any:
    """Выполняет синхронный запрос веб-интерфейса к базе данных в фоновом потоке."""
    if action == "fetch_bundles":
        return db.fetch_bundles(
            only_blocked=params["only_blocked"],
            since_delta=params["since_delta"],
            filter_host=params.get("filter_host", ""),
            filter_unit=params.get("filter_unit", ""),
            filter_container=params.get("filter_container", ""),
            search_msg=params.get("search_msg", ""),
            page=params.get("page", 1),
            page_size=params.get("page_size", 25),
        )
    elif action == "fetch_filter_options":
        return db.fetch_filter_options()
    elif action == "fetch_bundle_by_id":
        return db.fetch_bundle_by_id(params["bundle_id"])

    elif action == "bulk_action":
        act = params["bulk_type"]
        ids = params["bundle_ids"]
        if act == "block":
            db.set_bundles_block_status(ids, is_blocked=True)
        elif act == "unblock":
            db.set_bundles_block_status(ids, is_blocked=False)
        elif act == "delete":
            db.delete_bundles(ids)
        return True
    elif action == "create_manual":
        clean_host = clean_text_pipeline(params["host"])
        clean_prog = clean_text_pipeline(params["program"])
        raw_m = params["message"].strip()
        if is_substring_pattern(raw_m):
            clean_msg = f"{{{{{clean_text_pipeline(raw_m[2:-2])}}}}}"
        else:
            clean_msg = clean_text_pipeline(raw_m)
        return db.create_manual_bundle(
            host=clean_host,
            service=params["service"].strip(),
            program=clean_prog,
            message=clean_msg,
            description=params["description"],
            is_blocked=params["is_blocked"],
        )
    elif action == "update_full":
        clean_host = clean_text_pipeline(params["host"])
        clean_prog = clean_text_pipeline(params["program"])
        raw_m = params["message"].strip()
        if is_substring_pattern(raw_m):
            clean_msg = f"{{{{{clean_text_pipeline(raw_m[2:-2])}}}}}"
        else:
            clean_msg = clean_text_pipeline(raw_m)
        db.update_full_bundle(
            bundle_id=params["bundle_id"],
            host=clean_host,
            service=params["service"].strip(),
            program=clean_prog,
            message=clean_msg,
            description=params["description"],
            is_blocked=params["is_blocked"],
        )
        return True
    elif action == "api_block":
        clean_host = clean_text_pipeline(params["host"])
        clean_service = params["service"].strip() or "unknown"
        clean_prog = clean_text_pipeline(params["program"])
        raw_m = params["message"].strip()
        if is_substring_pattern(raw_m):
            clean_msg = f"{{{{{clean_text_pipeline(raw_m[2:-2])}}}}}"
        else:
            clean_msg = clean_text_pipeline(raw_m)
        return db.get_or_create_and_block_bundle(clean_host, clean_service, clean_prog, clean_msg)

    raise ValueError(f"Неизвестное действие UI: {action}")


def _bg_worker_loop() -> None:
    """Основной цикл фонового потока обработки задач и периодического сброса БД на диск."""
    logger.debug("Поток бэкграунда запущен.")
    last_flush = time.monotonic()

    try:
        while True:
            flush_interval = max(10, get_memory_flush_seconds())
            now = time.monotonic()
            wait_timeout = max(1.0, flush_interval - (now - last_flush))

            try:
                task = bg_queue.get(timeout=wait_timeout)
            except queue.Empty:
                db.flush_to_disk(force=False)
                last_flush = time.monotonic()
                continue
            except queue.ShutDown:
                logger.debug("Очередь бэкграунда полностью вычитана и закрыта.")
                break

            try:
                if task["source"] == "vector":
                    _handle_vector_batch(task["payload"])
                elif task["source"] == "ui":
                    reply_q: queue.Queue = task["reply_q"]
                    try:
                        res = _handle_ui_task(task["action"], task["params"])
                        reply_q.put(("ok", res))
                    except Exception as ui_err:
                        reply_q.put(("error", ui_err))
            except Exception as e:
                logger.error("Критическая ошибка в потоке бэкграунда: %s", e, exc_info=True)
            finally:
                bg_queue.task_done()

            if (time.monotonic() - last_flush) >= max(10, get_memory_flush_seconds()):
                db.flush_to_disk(force=False)
                last_flush = time.monotonic()
    finally:
        logger.debug("Закрытие базы данных при завершении потока бэкграунда...")
        db.close()


def start_background_worker() -> None:
    """Инициализирует базу данных и запускает фоновый рабочий поток."""
    global _bg_thread
    db.init()
    _bg_thread = threading.Thread(target=_bg_worker_loop, name="BackgroundWorker", daemon=False)
    _bg_thread.start()


def stop_background_worker() -> None:
    """Корректно закрывает очередь бэкграунда и ожидает завершения рабочего потока."""
    logger.info("Закрытие очереди бэкграунда и ожидание завершения его потока...")
    bg_queue.shutdown(immediate=False)
    if _bg_thread and _bg_thread.is_alive():
        _bg_thread.join(timeout=20)
    logger.info("Поток бэкграунда корректно остановлен.")
