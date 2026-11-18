"""Модуль формирования и отправки сводных почтовых уведомлений.

Обеспечивает накопление событий в течение настроенной задержки (`batch_delay_seconds`),
группировку алертов по хостам и службам, рендеринг локализованных HTML- и текстовых
шаблонов Jinja2 из директории `app/templates/mail` и отправку писем по SMTP.
"""

import logging
import queue
import smtplib
import ssl
import threading
import time
from collections import OrderedDict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import (
    get_base_url,
    get_batch_delay_seconds,
    get_first_available_api_key,
    get_language,
    get_smtp_config,
)
from app.i18n import translate
from app.worker import mail_queue

logger = logging.getLogger("alert_filter.mailer")

MAIL_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "mail"

_mail_jinja_env = Environment(
    loader=FileSystemLoader(str(MAIL_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_mail_jinja_env.globals["t"] = translate

_mail_thread: Optional[threading.Thread] = None


def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    """Создает SSL-контекст для SMTP-соединения с учетом проверки сертификата."""
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.debug("Проверка SSL-сертификата отключена согласно настройкам (verify_ssl=false).")
    return ctx


def _make_block_url(base_url: str, api_key: str, item: dict) -> str:
    """Формирует URL для быстрой блокировки связки через `/api/block`."""
    query_params = urlencode({
        "key": api_key,
        "host": item["host"],
        "service": item["service"],
        "program": item["program"],
        "message": item["message"],
    })
    return f"{base_url}/api/block?{query_params}"


def _make_link_label(item: dict, lang: str) -> str:
    """Формирует локализованную подпись ссылки быстрой блокировки."""
    msg_preview = item["message"].strip()
    if len(msg_preview) > 60:
        msg_preview = msg_preview[:57] + "..."
    return translate("mail_block_link", lang=lang, program=item["program"], preview=msg_preview)


def _prepare_item_payload(item: dict, base_url: str, api_key: str, lang: str) -> dict:
    """Подготавливает словарь данных одного события для передачи в шаблон письма."""
    return {
        "message": item["message"],
        "block_url": _make_block_url(base_url, api_key, item),
        "link_label": _make_link_label(item, lang),
    }


def _send_host_email(host: str, host_alerts: list, base_url: str, api_key: str) -> None:
    """Формирует и отправляет сводное письмо для конкретного хоста через шаблоны Jinja2."""
    lang = get_language()
    unique_programs = list(OrderedDict.fromkeys(item["program"] for item in host_alerts))
    programs_str = ", ".join(unique_programs)
    total_count = len(host_alerts)
    subject = translate(
        "mail_subject",
        lang=lang,
        host=host,
        programs=programs_str,
        count=total_count,
    )

    grouped: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    for item in host_alerts:
        group_key = (item["service"], item["program"])
        grouped.setdefault(group_key, []).append(item)

    template_groups = []
    for (service, program), items in grouped.items():
        first_item = _prepare_item_payload(items[0], base_url, api_key, lang)
        extra_items = [
            _prepare_item_payload(extra, base_url, api_key, lang) for extra in items[1:]
        ]
        template_groups.append({
            "service": service,
            "program": program,
            "first_item": first_item,
            "extra_items": extra_items,
        })

    context = {
        "lang": lang,
        "subject": subject,
        "host": host,
        "groups": template_groups,
        "current_year": datetime.now().year,
    }

    html_template = _mail_jinja_env.get_template("alert_email.html")
    text_template = _mail_jinja_env.get_template("alert_email.txt")

    full_html = html_template.render(**context)
    full_text = text_template.render(**context)

    logger.debug(
        "Сформировано письмо для хоста %s (групп: %d, всего событий: %d).",
        host,
        len(template_groups),
        total_count,
    )
    send_smtp(subject, full_html, full_text)


def build_and_send_alerts_email(alerts_to_send: list) -> None:
    """Группирует накопленные алерты по хостам и инициирует отправку писем."""
    if not alerts_to_send:
        return

    api_key = get_first_available_api_key()
    base_url = get_base_url()

    by_host: OrderedDict[str, list] = OrderedDict()
    for item in alerts_to_send:
        by_host.setdefault(item["host"], []).append(item)

    for host, host_alerts in by_host.items():
        _send_host_email(host, host_alerts, base_url, api_key)


def send_smtp(subject: str, html_body: str, text_body: str) -> None:
    """Выполняет отправку multipart/alternative письма через настроенный SMTP-сервер."""
    smtp_cfg = get_smtp_config()
    host = smtp_cfg.get("host", "localhost")
    port = int(smtp_cfg.get("port", 465))
    use_ssl = bool(smtp_cfg.get("use_ssl", True))
    use_starttls = bool(smtp_cfg.get("use_starttls", False))
    verify_ssl = bool(smtp_cfg.get("verify_ssl", True))
    from_addr = smtp_cfg["from_addr"]

    sender_domain = from_addr.split("@")[-1] if "@" in from_addr else "nas.lan"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(smtp_cfg["to_addrs"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_domain)

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        ssl_context = _build_ssl_context(verify_ssl)
        if use_ssl:
            logger.debug("Подключение к SMTP %s:%d по SSL...", host, port)
            server = smtplib.SMTP_SSL(host, port, context=ssl_context, timeout=15)
        else:
            logger.debug("Подключение к SMTP %s:%d...", host, port)
            server = smtplib.SMTP(host, port, timeout=15)
            if use_starttls:
                logger.debug("Активация STARTTLS...")
                server.starttls(context=ssl_context)

        with server:
            if smtp_cfg.get("user"):
                server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.sendmail(from_addr, smtp_cfg["to_addrs"], msg.as_string())

        logger.info("Сводное уведомление успешно отправлено на почту (%s).", ", ".join(smtp_cfg["to_addrs"]))
    except Exception as e:
        logger.error("Ошибка при отправке письма через SMTP (%s:%d): %s", host, port, e, exc_info=True)


def _mailer_worker_loop() -> None:
    """Основной цикл фонового потока почтовика с накоплением батчей по таймеру."""
    logger.debug("Поток почтовика запущен.")
    while True:
        try:
            first_batch = mail_queue.get()
        except queue.ShutDown:
            logger.debug("Очередь почтовика полностью вычитана и закрыта.")
            break

        combined_alerts = list(first_batch)
        mail_queue.task_done()

        delay = get_batch_delay_seconds()
        stop_requested = False

        if delay > 0:
            logger.debug("Сбор событий в течение %.1f сек. перед отправкой письма...", delay)
            deadline = time.monotonic() + delay

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    next_item = mail_queue.get(timeout=remaining)
                    combined_alerts.extend(next_item)
                    mail_queue.task_done()
                    logger.debug("К текущему письму добавлена пачка (всего событий: %d).", len(combined_alerts))
                except queue.Empty:
                    break
                except queue.ShutDown:
                    stop_requested = True
                    break

        try:
            build_and_send_alerts_email(combined_alerts)
        except Exception as e:
            logger.error("Критическая ошибка в потоке почтовика при отправке: %s", e, exc_info=True)

        if stop_requested:
            logger.debug("Поток почтовика завершает работу после отправки финального письма.")
            break


def start_mailer_worker() -> None:
    """Запускает фоновый поток отправки почтовых уведомлений."""
    global _mail_thread
    _mail_thread = threading.Thread(target=_mailer_worker_loop, name="MailerWorker", daemon=False)
    _mail_thread.start()


def stop_mailer_worker() -> None:
    """Корректно закрывает очередь почтовика и ожидает завершения отправки писем."""
    logger.info("Закрытие очереди почтовика и ожидание отправки накопленных писем...")
    mail_queue.shutdown(immediate=False)
    if _mail_thread and _mail_thread.is_alive():
        _mail_thread.join(timeout=30)
    logger.info("Поток почтовика корректно остановлен.")
