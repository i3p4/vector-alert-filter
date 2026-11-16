import hashlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("alert_filter.config")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "alerts.db"

_config_lock = threading.Lock()

ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "listen_host",
    "listen_port",
    "log_level",
    "db_backend",
    "memory_flush_seconds",
    "retention_days",
    "batch_delay_seconds",
    "page_size",
    "base_url",
    "smtp",
})

ALLOWED_SMTP_KEYS = frozenset({
    "host",
    "port",
    "use_ssl",
    "use_starttls",
    "verify_ssl",
    "user",
    "password",
    "from_addr",
    "to_addrs",
})

FORBIDDEN_KEYS = frozenset({
    "users",
    "username",
    "api_key",
})


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Файл конфигурации не найден по пути: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()


def get_listen_host() -> str:
    return str(CONFIG.get("listen_host", "0.0.0.0"))


def get_listen_port() -> int:
    return int(CONFIG.get("listen_port", 8080))


def get_log_level() -> str:
    return str(CONFIG.get("log_level", "INFO")).upper()


def get_db_backend() -> str:
    return str(CONFIG.get("db_backend", "sqlite_disk")).lower().strip()


def get_memory_flush_seconds() -> int:
    return int(CONFIG.get("memory_flush_seconds", 3600))


def get_retention_days() -> int:
    return int(CONFIG.get("retention_days", 30))


def get_batch_delay_seconds() -> float:
    return float(CONFIG.get("batch_delay_seconds", 300.0))


def get_page_size() -> int:
    try:
        val = int(CONFIG.get("page_size", 25))
        return max(1, val)
    except (TypeError, ValueError):
        return 25


def get_base_url() -> str:
    return str(CONFIG.get("base_url", "")).rstrip("/")


def get_smtp_config() -> dict:
    return CONFIG.get("smtp", {})


def get_session_secret() -> str:
    raw = json.dumps(CONFIG, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def find_user_by_credentials(username: str, password: str) -> Optional[dict]:
    for u in CONFIG.get("users", []):
        if u.get("username") == username and u.get("password") == password:
            return u
    return None


def find_user_by_username(username: str) -> Optional[dict]:
    for u in CONFIG.get("users", []):
        if u.get("username") == username:
            return u
    return None


def find_user_by_api_key(api_key: str) -> Optional[dict]:
    if not api_key:
        return None
    for u in CONFIG.get("users", []):
        if u.get("api_key") == api_key:
            return u
    return None


def get_first_available_api_key() -> str:
    for u in CONFIG.get("users", []):
        if u.get("api_key"):
            return str(u["api_key"])
    return ""


def get_editable_settings() -> dict:
    with _config_lock:
        smtp = CONFIG.get("smtp", {})
        to_addrs = smtp.get("to_addrs", [])
        if isinstance(to_addrs, list):
            to_addrs_str = ", ".join(str(addr) for addr in to_addrs)
        else:
            to_addrs_str = str(to_addrs)

        return {
            "listen_host": get_listen_host(),
            "listen_port": get_listen_port(),
            "log_level": get_log_level(),
            "db_backend": get_db_backend(),
            "memory_flush_seconds": get_memory_flush_seconds(),
            "retention_days": get_retention_days(),
            "batch_delay_seconds": get_batch_delay_seconds(),
            "page_size": get_page_size(),
            "base_url": get_base_url(),
            "smtp_host": str(smtp.get("host", "localhost")),
            "smtp_port": int(smtp.get("port", 465)),
            "smtp_use_ssl": bool(smtp.get("use_ssl", True)),
            "smtp_use_starttls": bool(smtp.get("use_starttls", False)),
            "smtp_verify_ssl": bool(smtp.get("verify_ssl", True)),
            "smtp_user": str(smtp.get("user", "")),
            "smtp_from_addr": str(smtp.get("from_addr", "")),
            "smtp_to_addrs": to_addrs_str,
        }


def update_allowed_settings(validated_data: dict[str, Any]):
    for key in validated_data:
        if key in FORBIDDEN_KEYS or key not in ALLOWED_TOP_LEVEL_KEYS:
            raise ValueError(f"Попытка изменения запрещенного параметра конфигурации: {key}")

    if "smtp" in validated_data:
        if not isinstance(validated_data["smtp"], dict):
            raise ValueError("Некорректный формат секции SMTP.")
        for smtp_key in validated_data["smtp"]:
            if smtp_key in FORBIDDEN_KEYS or smtp_key not in ALLOWED_SMTP_KEYS:
                raise ValueError(f"Попытка изменения запрещенного параметра SMTP: {smtp_key}")

    with _config_lock:
        updated_cfg = dict(CONFIG)

        for key, value in validated_data.items():
            if key == "smtp":
                merged_smtp = dict(updated_cfg.get("smtp", {}))
                for s_key, s_val in value.items():
                    if s_key == "password" and not s_val:
                        continue
                    merged_smtp[s_key] = s_val
                updated_cfg["smtp"] = merged_smtp
            else:
                updated_cfg[key] = value

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(DATA_DIR), prefix="config_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(updated_cfg, tmp_file, ensure_ascii=False, indent=2)
                tmp_file.write("\n")
            os.replace(tmp_path, CONFIG_PATH)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        CONFIG.clear()
        CONFIG.update(updated_cfg)
        logger.info("Настройки приложения успешно обновлены и сохранены в %s.", CONFIG_PATH)
