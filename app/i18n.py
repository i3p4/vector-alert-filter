"""Модуль интернационализации и локализации (i18n) для приложения.

Обеспечивает загрузку словарей переводов из JSON-файлов в директории `app/locales`,
получение списка доступных языков и перевод текстовых ключей с подстановкой параметров.
"""

import json
import logging
from pathlib import Path
from typing import Dict

from app.config import get_language

logger = logging.getLogger("alert_filter.i18n")

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT_LANGUAGE = "ru"

_translations_cache: Dict[str, Dict[str, str]] = {}


def _load_locale_file(lang_code: str) -> Dict[str, str]:
    """Загружает словарь переводов из JSON-файла для указанного кода языка.

    Args:
        lang_code: Двухбуквенный или составной код языка (например, 'ru', 'en').

    Returns:
        Словарь вида {ключ_перевода: переведенная_строка}. Если файл не найден
        или поврежден, возвращается пустой словарь.
    """
    safe_code = Path(lang_code).name
    file_path = LOCALES_DIR / f"{safe_code}.json"
    if not file_path.is_file():
        logger.warning("Файл локализации не найден: %s", file_path)
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.error("Некорректный формат файла локализации %s: ожидался JSON-объект.", file_path)
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:
        logger.error("Ошибка чтения файла локализации %s: %s", file_path, exc, exc_info=True)
        return {}


def get_translations(lang_code: str) -> Dict[str, str]:
    """Возвращает кэшированный словарь переводов для языка.

    Args:
        lang_code: Код запрашиваемого языка.

    Returns:
        Словарь переводов для указанного языка.
    """
    if lang_code not in _translations_cache:
        _translations_cache[lang_code] = _load_locale_file(lang_code)
    return _translations_cache[lang_code]


def get_available_languages() -> Dict[str, str]:
    """Сканирует директорию локалей и возвращает список доступных языков.

    Каждый файл `*.json` должен содержать специальный ключ `_language_name`
    для человекочитаемого отображения в настройках (например, 'Русский', 'English').

    Returns:
        Словарь вида {код_языка: отображаемое_название}, отсортированный по коду.
    """
    languages: Dict[str, str] = {}
    if not LOCALES_DIR.is_dir():
        return {DEFAULT_LANGUAGE: "Русский"}

    for file_path in sorted(LOCALES_DIR.glob("*.json")):
        code = file_path.stem
        translations = get_translations(code)
        display_name = translations.get("_language_name", code.upper())
        languages[code] = display_name

    if not languages:
        languages[DEFAULT_LANGUAGE] = "Русский"
    return languages


def translate(key: str, lang: str | None = None, **kwargs) -> str:
    """Переводит строковый ключ на текущий или указанный язык с форматированием.

    Если ключ отсутствует в целевом языке, выполняется откат к языку по умолчанию
    (`DEFAULT_LANGUAGE`), а при отсутствии и там — возвращается сам ключ.

    Args:
        key: Идентификатор строки в словаре локализации.
        lang: Явный код языка. Если None, берется активный язык из конфигурации.
        **kwargs: Именованные аргументы для подстановки через `str.format()`.

    Returns:
        Переведенная и отформатированная строка.
    """
    active_lang = lang or get_language()
    target_dict = get_translations(active_lang)
    text = target_dict.get(key)

    if text is None and active_lang != DEFAULT_LANGUAGE:
        fallback_dict = get_translations(DEFAULT_LANGUAGE)
        text = fallback_dict.get(key)

    if text is None:
        text = key

    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError) as exc:
            logger.warning("Ошибка форматирования ключа локализации '%s': %s", key, exc)
            return text
    return text
