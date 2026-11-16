#!/usr/bin/env python3

import argparse
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    get_listen_host,
    get_listen_port,
    get_log_level,
    get_session_secret,
)
from app.mailer import start_mailer_worker, stop_mailer_worker
from app.web import router
from app.worker import start_background_worker, stop_background_worker


def parse_args():
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


def setup_logging(level_str: str):
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


args = parse_args()

effective_log_level = (args.log_level or get_log_level()).upper()
effective_host = args.host or get_listen_host()
effective_port = args.port or get_listen_port()

setup_logging(effective_log_level)
logger = logging.getLogger("alert_filter.main")


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    logger.info("Запуск рабочих потоков бэкграунда и почтовика...")
    start_background_worker()
    start_mailer_worker()
    yield
    logger.info("Получен сигнал завершения работы приложения. Начинаем корректную остановку...")
    stop_background_worker()
    stop_mailer_worker()
    logger.info("Все очереди обработаны, база сохранена, приложение полностью остановлено.")


app = FastAPI(title="Фильтр уведомлений Vector", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=get_session_secret())
app.include_router(router)

if __name__ == "__main__":
    logger.debug(
        "Параметры запуска сервера: host=%s, port=%d, log_level=%s",
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
    )
