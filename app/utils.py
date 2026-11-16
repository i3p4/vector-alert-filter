import hashlib
import logging
import re

logger = logging.getLogger("alert_filter.utils")

ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

DATETIME_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
    r"\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?",
    r"\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}",
    r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\b\d{2}\.\d{2}\.\d{4}\b",
    r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b",
    r"\b\d{10,13}\b",
]


def decolorize(text: str) -> str:
    if not text:
        return ""
    cleaned = ANSI_ESCAPE_RE.sub("", str(text))
    logger.debug("Результат после удаления ANSI-цветов: %r", cleaned)
    return cleaned


def strip_datetime(text: str) -> str:
    if not text:
        return ""
    cleaned = str(text)
    for pattern in DATETIME_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    result = re.sub(r"\s+", " ", cleaned).strip()
    logger.debug("Результат после удаления дат и времени: %r", result)
    return result


def clean_text_pipeline(raw_text: str) -> str:
    logger.debug("Исходная строка до очистки: %r", raw_text)
    no_color = decolorize(raw_text)
    no_dt = strip_datetime(no_color)
    return no_dt


def make_bundle_hash(host: str, service: str, program: str, message: str) -> str:
    raw = f"{host.lower()}|{service.lower()}|{program.lower()}|{message.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_substring_pattern(pattern_msg: str) -> bool:
    return (
        len(pattern_msg) > 4
        and pattern_msg.startswith("{{")
        and pattern_msg.endswith("}}")
    )


def match_message(stored_msg: str, incoming_msg: str) -> bool:
    if is_substring_pattern(stored_msg):
        sub = stored_msg[2:-2].strip()
        matched = sub.lower() in incoming_msg.lower()
        logger.debug(
            "Проверка частичного совпадения (подстрока %r в %r): %s",
            sub,
            incoming_msg,
            matched,
        )
        return matched
    return stored_msg.lower() == incoming_msg.lower()


def extract_clean_vector_event(event: dict) -> dict:
    logger.debug("Разбор события из Vector: %s", event)

    raw_host = str(event.get("host") or "unknown-host")
    raw_container = str(event.get("container") or "").strip()
    raw_unit = str(event.get("unit") or "").strip()

    if raw_container:
        service = "container"
        raw_program = raw_container
    elif raw_unit:
        service = "unit"
        raw_program = raw_unit
    else:
        service = "unknown"
        raw_program = str(event.get("program") or "unknown-program")

    raw_msg = str(event.get("message") or "")

    host = clean_text_pipeline(raw_host)
    program = clean_text_pipeline(raw_program)
    clean_message = clean_text_pipeline(raw_msg)

    b_hash = make_bundle_hash(host, service, program, clean_message)

    result = {
        "host": host,
        "service": service,
        "program": program,
        "message": clean_message,
        "bundle_hash": b_hash,
    }
    logger.debug("Сформирована очищенная структура события Vector: %s", result)
    return result
