import logging
import queue
import smtplib
import ssl
import threading
import time
from collections import OrderedDict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from html import escape
from typing import Optional
from urllib.parse import urlencode

from app.config import (
    get_base_url,
    get_batch_delay_seconds,
    get_first_available_api_key,
    get_smtp_config,
)
from app.worker import mail_queue

logger = logging.getLogger("alert_filter.mailer")

_mail_thread: Optional[threading.Thread] = None


def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.debug("Проверка SSL-сертификата отключена согласно настройкам (verify_ssl=false).")
    return ctx


def _make_block_url(base_url: str, api_key: str, item: dict) -> str:
    query_params = urlencode({
        "key": api_key,
        "host": item["host"],
        "service": item["service"],
        "program": item["program"],
        "message": item["message"],
    })
    return f"{base_url}/api/block?{query_params}"


def build_and_send_alerts_email(alerts_to_send: list):
    if not alerts_to_send:
        return

    api_key = get_first_available_api_key()
    base_url = get_base_url()

    grouped = OrderedDict()
    for item in alerts_to_send:
        group_key = (item["host"], item["service"], item["program"])
        if group_key not in grouped:
            grouped[group_key] = []
        grouped[group_key].append(item)

    html_groups = []
    text_groups = []

    for (host, service, program), items in grouped.items():
        first_item = items[0]
        first_link = _make_block_url(base_url, api_key, first_item)

        group_html_parts = [
            "<article>",
            f"<h2>{escape(host)} — [{escape(service)}] {escape(program)}</h2>",
            f"<p><strong>Сообщение:</strong><br>{escape(first_item['message'])}<br>"
            f'<a href="{escape(first_link)}">Заблокировать эту связку</a></p>',
        ]

        group_text_parts = [
            f"host: {host} | {service}: {program}",
            f"  - Сообщение: {first_item['message']}",
            f"    Заблокировать: {first_link}",
        ]

        if len(items) > 1:
            remaining_items = items[1:]
            spoiler_entries = []
            for extra in remaining_items:
                extra_link = _make_block_url(base_url, api_key, extra)
                spoiler_entries.append(
                    f"<p>{escape(extra['message'])}<br>"
                    f'<a href="{escape(extra_link)}">Заблокировать эту связку</a></p>'
                )
                group_text_parts.append(
                    f"  - Сообщение: {extra['message']}\n"
                    f"    Заблокировать: {extra_link}"
                )

            group_html_parts.append(
                f"<details>"
                f"<summary>Еще ({len(remaining_items)}) сообщений</summary>"
                f"{''.join(spoiler_entries)}"
                f"</details>"
            )

        group_html_parts.append("</article><hr>")
        group_text_parts.append("---")

        html_groups.append("\n".join(group_html_parts))
        text_groups.append("\n".join(group_text_parts))

    subject = f"Новые события Vector ({len(alerts_to_send)})"
    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>{escape(subject)}</title>
</head>
<body>
    <h1>{escape(subject)}</h1>
    {''.join(html_groups)}
</body>
</html>"""

    logger.debug(
        "Сформировано сгруппированное HTML-письмо (групп: %d, всего событий: %d).",
        len(grouped),
        len(alerts_to_send),
    )
    send_smtp(subject, full_html, "\n".join(text_groups))


def send_smtp(subject: str, html_body: str, text_body: str):
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


def _mailer_worker_loop():
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


def start_mailer_worker():
    global _mail_thread
    _mail_thread = threading.Thread(target=_mailer_worker_loop, name="MailerWorker", daemon=False)
    _mail_thread.start()


def stop_mailer_worker():
    logger.info("Закрытие очереди почтовика и ожидание отправки накопленных писем...")
    mail_queue.shutdown(immediate=False)
    if _mail_thread and _mail_thread.is_alive():
        _mail_thread.join(timeout=30)
    logger.info("Поток почтовика корректно остановлен.")
