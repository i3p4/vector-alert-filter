#!/usr/bin/env python3
"""Точка входа сервиса фильтрации уведомлений Vector.

Инициализирует логирование, настраивает FastAPI-приложение и сессии, проверяет
параметры SSL/TLS при включенном HTTPS и запускает встроенный сервер Uvicorn
с корректным управлением жизненным циклом фоновых рабочих потоков.
"""

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    get_listen_host,
    get_listen_port,
    get_log_level,
    get_session_secret,
    get_ssl_cert_filename,
    get_ssl_key_filename,
    is_https_enabled,
    resolve_data_file,
)
from app.mailer import start_mailer_worker, stop_mailer_worker
from app.web import router
from app.worker import start_background_worker, stop_background_worker


def parse_args() -> argparse.Namespace:
    """Разбирает аргументы командной строки для переопределения параметров запуска.

    Returns:
        Объект `argparse.Namespace` с переданными флагами `--host`, `--port` и `--log-level`.
    """
    parser = argparse.ArgumentParser(description="Сервис фильтрации уведомлений Vector")
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="IP-адрес для прослушивания (переопределяет config.json)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Порт для прослушивания (переопределяет config.json)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        choices=["INFO", "ERROR", "DEBUG", "info", "error", "debug"],
        default=None,
        help="Уровень логирования: INFO, ERROR или DEBUG (переопределяет config.json)",
    )
    return parser.parse_args()


def setup_logging(level_str: str) -> None:
    """Настраивает корневой логгер приложения с выводом в стандартный поток.

    Args:
        level_str: Строковое имя уровня логирования ('DEBUG', 'INFO' или 'ERROR').
    """
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "ERROR": logging.ERROR,
    }
    numeric_level = level_map.get(level_str.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def resolve_ssl_paths() -> tuple[Optional[str], Optional[str]]:
    """Проверяет конфигурацию HTTPS и возвращает пути к сертификату и приватному ключу.

    При включенном HTTPS проверяет, что имена файлов указаны корректно (без путей)
    и оба файла существуют в директории `data/`. В случае ошибки записывает понятное
    сообщение в журнал и завершает процесс с кодом 1.

    Returns:
        Кортеж `(ssl_certfile, ssl_keyfile)` со строковыми путями или `(None, None)`,
        если HTTPS отключен.
    """
    if not is_https_enabled():
        return None, None

    cert_name = get_ssl_cert_filename()
    key_name = get_ssl_key_filename()

    if not cert_name or not key_name:
        logger.error(
            "Ошибка запуска HTTPS: в конфигурации включен https_enabled, но не заданы "
            "имена файлов ssl_certfile и/или ssl_keyfile."
        )
        sys.exit(1)

    try:
        cert_path = resolve_data_file(cert_name)
        key_path = resolve_data_file(key_name)
    except ValueError as exc:
        logger.error(
            "Ошибка запуска HTTPS: некорректное имя файла сертификата или ключа (%s). "
            "Указывайте только имена файлов внутри папки data/.",
            exc,
        )
        sys.exit(1)

    if not cert_path.is_file():
        logger.error(
            "Ошибка запуска HTTPS: файл SSL-сертификата не найден по пути: %s",
            cert_path,
        )
        sys.exit(1)

    if not key_path.is_file():
        logger.error(
            "Ошибка запуска HTTPS: файл приватного ключа SSL не найден по пути: %s",
            key_path,
        )
        sys.exit(1)

    return str(cert_path), str(key_path)


args = parse_args()

effective_log_level = (args.log_level or get_log_level()).upper()
effective_host = args.host or get_listen_host()
effective_port = args.port or get_listen_port()
https_active = is_https_enabled()

setup_logging(effective_log_level)
logger = logging.getLogger("alert_filter.main")


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Управляет запуском и корректной остановкой фоновых рабочих потоков."""
    logger.info("Запуск рабочих потоков бэкграунда и почтовика...")
    start_background_worker()
    start_mailer_worker()
    yield
    logger.info("Получен сигнал завершения работы приложения. Начинаем корректную остановку...")
    stop_background_worker()
    stop_mailer_worker()
    logger.info("Все очереди обработаны, база сохранена, приложение полностью остановлено.")


app = FastAPI(title="Фильтр уведомлений Vector", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_session_secret(),
    https_only=https_active,
)
app.include_router(router)

if __name__ == "__main__":
    ssl_certfile, ssl_keyfile = resolve_ssl_paths()
    protocol = "HTTPS" if ssl_certfile and ssl_keyfile else "HTTP"
    logger.info(
        "Запуск сервера в режиме %s на %s:%d (уровень логов: %s).",
        protocol,
        effective_host,
        effective_port,
        effective_log_level,
    )
    uvicorn_log_level = "debug" if effective_log_level == "DEBUG" else "error"
    uvicorn.run(
        app,
        host=effective_host,
        port=effective_port,
        log_level=uvicorn_log_level,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )
