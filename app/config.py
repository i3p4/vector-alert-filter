import hashlib
import json
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "alerts.db"


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
