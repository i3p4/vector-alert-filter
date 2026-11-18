"""Модуль маршрутизации веб-интерфейса и HTTP API приложения.

Обеспечивает рендеринг HTML-шаблонов Jinja2 из директории `app/templates/web`
с поддержкой локализации, аутентификацию пользователей по сессиям, прием вебхуков
от Vector, быструю блокировку по API-ключу, фильтрацию, пагинацию и управление
настройками приложения (включая язык и параметры HTTPS).
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    find_user_by_api_key,
    find_user_by_credentials,
    find_user_by_username,
    get_editable_settings,
    get_language,
    get_page_size,
    get_retention_days,
    resolve_data_file,
    update_allowed_settings,
)
from app.i18n import get_available_languages, translate
from app.utils import format_local_datetime, is_substring_pattern
from app.worker import submit_ui_task, submit_vector_payload

logger = logging.getLogger("alert_filter.web")
router = APIRouter()

WEB_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "web"
templates = Jinja2Templates(directory=str(WEB_TEMPLATES_DIR))
templates.env.globals["t"] = translate

PERIOD_DELTAS: dict[str, Optional[timedelta]] = {
    "1h": timedelta(hours=1),
    "3h": timedelta(hours=3),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "15d": timedelta(days=15),
    "30d": timedelta(days=30),
    "all": None,
}

ALLOWED_SETTINGS_FORM_FIELDS = frozenset({
    "language",
    "listen_host",
    "listen_port",
    "log_level",
    "https_enabled",
    "ssl_certfile",
    "ssl_keyfile",
    "db_backend",
    "memory_flush_seconds",
    "retention_days",
    "batch_delay_seconds",
    "page_size",
    "base_url",
    "smtp_host",
    "smtp_port",
    "smtp_use_ssl",
    "smtp_use_starttls",
    "smtp_verify_ssl",
    "smtp_user",
    "smtp_password",
    "smtp_from_addr",
    "smtp_to_addrs",
})


def get_current_user(request: Request) -> Optional[dict]:
    """Возвращает словарь данных текущего авторизованного пользователя или None."""
    username = request.session.get("user")
    if not username:
        return None
    return find_user_by_username(username)


def render_template(
    request: Request,
    template_name: str,
    page_title: str,
    user: Optional[dict] = None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    """Рендерит HTML-шаблон Jinja2 с общими переменными контекста.

    Args:
        request: Текущий HTTP-запрос FastAPI.
        template_name: Имя файла шаблона внутри `app/templates/web`.
        page_title: Локализованный заголовок страницы.
        user: Словарь текущего пользователя или None для неавторизованных страниц.
        status_code: HTTP-код ответа.
        **context: Дополнительные переменные для передачи в шаблон.

    Returns:
        Готовый HTML-ответ (`HTMLResponse`).
    """
    base_context = {
        "lang": get_language(),
        "page_title": page_title,
        "user": user,
        "current_year": datetime.now().year,
    }
    base_context.update(context)
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=base_context,
        status_code=status_code,
    )

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    """Отображает страницу входа в систему."""
    return render_template(
        request,
        "login.html",
        translate("login_title"),
        error=error,
    )


@router.post("/login")
def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    """Обрабатывает отправку формы авторизации пользователя."""
    user = find_user_by_credentials(username, password)
    if user:
        request.session["user"] = user["username"]
        logger.debug("Пользователь %s успешно вошел в веб-интерфейс.", username)
        return RedirectResponse("/", status_code=303)
    logger.debug("Неудачная попытка входа для пользователя %s.", username)
    return login_page(request, translate("login_error_invalid"))


@router.get("/logout")
def logout_action(request: Request):
    """Очищает текущую сессию пользователя и перенаправляет на страницу входа."""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/webhook/vector")
def vector_webhook(payload: Any = Body(...)):
    """Принимает пакет событий от Vector и помещает его в фоновую очередь."""
    logger.info("Фронтенд принял пакет данных от Vector и передал в очередь бэкграунда.")
    submit_vector_payload(payload)
    return {"status": "accepted"}


@router.get("/api/block", response_class=HTMLResponse)
def api_block_bundle(
    request: Request,
    key: str = Query(...),
    host: str = Query(...),
    service: str = Query("unknown"),
    program: str = Query(...),
    message: str = Query(...),
):
    """Блокирует связку по прямой ссылке из письма с проверкой API-ключа."""
    valid_user = find_user_by_api_key(key)
    if not valid_user:
        logger.error("Попытка вызова API блокировки с неверным ключом.")
        raise HTTPException(status_code=403, detail=translate("api_invalid_key"))

    request.session["user"] = valid_user["username"]

    bundle_id = submit_ui_task(
        "api_block",
        host=host,
        service=service,
        program=program,
        message=message,
    )
    return RedirectResponse(f"/bundle/{bundle_id}?msg=blocked", status_code=303)


def _build_page_url(
    target_page: int,
    period: str,
    filter_host: str,
    filter_unit: str,
    filter_container: str,
    search_msg: str,
    only_blocked: int,
) -> str:
    """Формирует URL страницы списка связок с сохранением активных фильтров."""
    params: dict[str, Any] = {"period": period, "page": target_page}
    if filter_host:
        params["filter_host"] = filter_host
    if filter_unit:
        params["filter_unit"] = filter_unit
    if filter_container:
        params["filter_container"] = filter_container
    if search_msg:
        params["search_msg"] = search_msg
    if only_blocked == 1:
        params["only_blocked"] = 1
    return "/?" + urlencode(params)


def _build_pagination_context(
    current_page: int,
    total_pages: int,
    period: str,
    filter_host: str,
    filter_unit: str,
    filter_container: str,
    search_msg: str,
    only_blocked: int,
) -> dict[str, Any]:
    """Подготавливает структуру данных для рендеринга навигации по страницам."""
    def page_href(p: int) -> str:
        return _build_page_url(
            p, period, filter_host, filter_unit, filter_container, search_msg, only_blocked
        )

    if total_pages <= 7:
        visible_pages: list[Optional[int]] = list(range(1, total_pages + 1))
    else:
        raw_pages = sorted({
            1,
            total_pages,
            max(1, current_page - 1),
            current_page,
            min(total_pages, current_page + 1),
        })
        visible_pages = []
        prev_p = 0
        for p in raw_pages:
            if prev_p and p - prev_p > 1:
                visible_pages.append(None)
            visible_pages.append(p)
            prev_p = p

    tokens = []
    for item in visible_pages:
        if item is None:
            tokens.append({"type": "ellipsis"})
        elif item == current_page:
            tokens.append({"type": "current", "page": item})
        else:
            tokens.append({"type": "link", "page": item, "url": page_href(item)})

    return {
        "current_page": current_page,
        "total_pages": total_pages,
        "first_url": page_href(1),
        "last_url": page_href(total_pages),
        "tokens": tokens,
    }

@router.get("/", response_class=HTMLResponse)
def index_page(
    request: Request,
    only_blocked: int = Query(0),
    period: str = Query("1h"),
    filter_host: str = Query(""),
    filter_unit: str = Query(""),
    filter_container: str = Query(""),
    search_msg: str = Query(""),
    page: int = Query(1, ge=1),
):
    """Отображает главную страницу со списком связок, фильтрами и пагинацией."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if period not in PERIOD_DELTAS:
        period = "1h"

    since_delta = PERIOD_DELTAS[period]
    clean_host = filter_host.strip()
    clean_unit = filter_unit.strip()
    clean_container = filter_container.strip()
    clean_search = search_msg.strip()

    filter_opts = submit_ui_task("fetch_filter_options")
    page_result = submit_ui_task(
        "fetch_bundles",
        only_blocked=(only_blocked == 1),
        since_delta=since_delta,
        filter_host=clean_host,
        filter_unit=clean_unit,
        filter_container=clean_container,
        search_msg=clean_search,
        page=page,
        page_size=get_page_size(),
    )

    total_count = page_result["total_count"]
    total_pages = page_result["total_pages"]
    current_page = page_result["current_page"]

    formatted_rows = []
    for r in page_result["items"]:
        row_dict = dict(r)
        row_dict["desc_short"] = r["description"] if r["description"] else r["message"][:80]
        row_dict["last_dt"] = format_local_datetime(r["last_triggered"])
        formatted_rows.append(row_dict)

    period_options = [(k, translate(f"period_{k}")) for k in PERIOD_DELTAS]
    pagination = _build_pagination_context(
        current_page=current_page,
        total_pages=total_pages,
        period=period,
        filter_host=clean_host,
        filter_unit=clean_unit,
        filter_container=clean_container,
        search_msg=clean_search,
        only_blocked=only_blocked,
    )

    return render_template(
        request,
        "index.html",
        translate("index_title"),
        user=user,
        period_options=period_options,
        period=period,
        filter_hosts=filter_opts.get("hosts", []),
        filter_units=filter_opts.get("units", []),
        filter_containers=filter_opts.get("containers", []),
        clean_host=clean_host,
        clean_unit=clean_unit,
        clean_container=clean_container,
        clean_search=clean_search,
        only_blocked=only_blocked,
        rows=formatted_rows,
        total_count=total_count,
        pagination=pagination,
    )


@router.post("/bulk-action")
def bulk_action(
    request: Request,
    action: str = Form(...),
    bundle_ids: List[int] = Form(default=[]),
    period: str = Form(default="1h"),
    filter_host: str = Form(default=""),
    filter_unit: str = Form(default=""),
    filter_container: str = Form(default=""),
    search_msg: str = Form(default=""),
    only_blocked: int = Form(default=0),
    page: int = Form(default=1),
):
    """Выполняет массовую блокировку, разблокировку или удаление выбранных связок."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if action in ("block", "unblock", "delete"):
        submit_ui_task("bulk_action", bulk_type=action, bundle_ids=bundle_ids)

    safe_period = period if period in PERIOD_DELTAS else "1h"
    safe_page = max(1, page)
    redirect_url = _build_page_url(
        target_page=safe_page,
        period=safe_period,
        filter_host=filter_host.strip(),
        filter_unit=filter_unit.strip(),
        filter_container=filter_container.strip(),
        search_msg=search_msg.strip(),
        only_blocked=(1 if only_blocked == 1 else 0),
    )
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/bundle/new", response_class=HTMLResponse)
def new_bundle_page(request: Request):
    """Отображает форму ручного создания новой связки."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return render_template(
        request,
        "bundle_new.html",
        translate("new_bundle_title"),
        user=user,
    )

@router.post("/bundle/new")
def create_bundle_action(
    request: Request,
    host: str = Form(...),
    service: str = Form(...),
    program: str = Form(...),
    message: str = Form(...),
    description: str = Form(default=""),
    is_blocked: int = Form(default=0),
):
    """Создает новую связку в базе данных по данным из веб-формы."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    bundle_id = submit_ui_task(
        "create_manual",
        host=host,
        service=service,
        program=program,
        message=message,
        description=description,
        is_blocked=(is_blocked == 1),
    )
    return RedirectResponse(f"/bundle/{bundle_id}?msg=saved", status_code=303)


@router.get("/bundle/{bundle_id}", response_class=HTMLResponse)
def bundle_detail(request: Request, bundle_id: int, msg: str = ""):
    """Отображает страницу просмотра и редактирования конкретной связки."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    row = submit_ui_task("fetch_bundle_by_id", bundle_id=bundle_id)
    if not row:
        raise HTTPException(status_code=404, detail=translate("detail_not_found"))

    return render_template(
        request,
        "bundle_detail.html",
        translate("detail_title", id=bundle_id, host=row["host"]),
        user=user,
        bundle=row,
        msg=msg,
        is_substring=is_substring_pattern(row["message"]),
        last_triggered_formatted=format_local_datetime(row["last_triggered"]),
        retention_days=get_retention_days(),
    )


@router.post("/bundle/{bundle_id}/update")
def bundle_update(
    request: Request,
    bundle_id: int,
    action: str = Form(...),
    host: str = Form(default=""),
    service: str = Form(default="unknown"),
    program: str = Form(default=""),
    message: str = Form(default=""),
    description: str = Form(default=""),
    is_blocked: int = Form(default=0),
):
    """Сохраняет изменения связки или удаляет ее из базы данных."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if action == "delete":
        submit_ui_task("bulk_action", bulk_type="delete", bundle_ids=[bundle_id])
        return RedirectResponse("/", status_code=303)

    if action == "save_all":
        submit_ui_task(
            "update_full",
            bundle_id=bundle_id,
            host=host,
            service=service,
            program=program,
            message=message,
            description=description,
            is_blocked=(is_blocked == 1),
        )
        return RedirectResponse(f"/bundle/{bundle_id}?msg=saved", status_code=303)

    return RedirectResponse(f"/bundle/{bundle_id}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    """Отображает страницу настроек приложения."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    return render_template(
        request,
        "settings.html",
        translate("settings_title"),
        user=user,
        cfg=get_editable_settings(),
        available_languages=get_available_languages(),
        msg=msg,
        error_message="",
    )

@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
    """Проверяет и сохраняет обновленные настройки приложения из веб-формы."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    form_data = await request.form()
    submitted_keys = set(form_data.keys())
    disallowed_keys = submitted_keys - ALLOWED_SETTINGS_FORM_FIELDS
    if disallowed_keys:
        logger.error(
            "Блокировка запроса на изменение настроек: обнаружены запрещенные или неизвестные поля: %s",
            sorted(disallowed_keys),
        )
        raise HTTPException(
            status_code=400,
            detail=translate("err_disallowed_fields"),
        )

    available_langs = get_available_languages()

    try:
        language = str(form_data.get("language", "ru")).strip().lower()
        if language not in available_langs:
            language = "ru"

        listen_host = str(form_data.get("listen_host", "")).strip()
        if not listen_host:
            raise ValueError(translate("err_empty_listen_host"))

        listen_port = int(str(form_data.get("listen_port", "")).strip())
        if not (1 <= listen_port <= 65535):
            raise ValueError(translate("err_invalid_listen_port"))

        log_level = str(form_data.get("log_level", "INFO")).strip().upper()
        if log_level not in ("DEBUG", "INFO", "ERROR"):
            raise ValueError(translate("err_invalid_log_level"))

        https_enabled = "https_enabled" in form_data
        ssl_certfile = str(form_data.get("ssl_certfile", "")).strip()
        ssl_keyfile = str(form_data.get("ssl_keyfile", "")).strip()

        if https_enabled:
            if not ssl_certfile or not ssl_keyfile:
                raise ValueError(translate("err_https_missing_files"))
            try:
                cert_path = resolve_data_file(ssl_certfile)
            except ValueError:
                raise ValueError(translate("err_https_invalid_filename", filename=ssl_certfile))
            try:
                key_path = resolve_data_file(ssl_keyfile)
            except ValueError:
                raise ValueError(translate("err_https_invalid_filename", filename=ssl_keyfile))

            if not cert_path.is_file():
                raise ValueError(translate("err_https_cert_not_found", filename=ssl_certfile))
            if not key_path.is_file():
                raise ValueError(translate("err_https_key_not_found", filename=ssl_keyfile))
        else:
            if ssl_certfile:
                try:
                    resolve_data_file(ssl_certfile)
                except ValueError:
                    raise ValueError(translate("err_https_invalid_filename", filename=ssl_certfile))
            if ssl_keyfile:
                try:
                    resolve_data_file(ssl_keyfile)
                except ValueError:
                    raise ValueError(translate("err_https_invalid_filename", filename=ssl_keyfile))

        db_backend = str(form_data.get("db_backend", "sqlite_disk")).strip().lower()
        if db_backend not in ("sqlite_disk", "sqlite_memory"):
            raise ValueError(translate("err_invalid_db_backend"))

        memory_flush_seconds = int(str(form_data.get("memory_flush_seconds", "")).strip())
        if memory_flush_seconds < 10:
            raise ValueError(translate("err_invalid_memory_flush"))

        retention_days = int(str(form_data.get("retention_days", "")).strip())
        if retention_days < 1:
            raise ValueError(translate("err_invalid_retention"))

        batch_delay_seconds = float(str(form_data.get("batch_delay_seconds", "")).strip())
        if batch_delay_seconds < 0:
            raise ValueError(translate("err_invalid_batch_delay"))

        page_size = int(str(form_data.get("page_size", "")).strip())
        if page_size < 1:
            raise ValueError(translate("err_invalid_page_size"))

        base_url = str(form_data.get("base_url", "")).strip().rstrip("/")

        smtp_host = str(form_data.get("smtp_host", "")).strip()
        if not smtp_host:
            raise ValueError(translate("err_empty_smtp_host"))

        smtp_port = int(str(form_data.get("smtp_port", "")).strip())
        if not (1 <= smtp_port <= 65535):
            raise ValueError(translate("err_invalid_smtp_port"))

        smtp_from_addr = str(form_data.get("smtp_from_addr", "")).strip()
        if not smtp_from_addr:
            raise ValueError(translate("err_empty_smtp_from"))

        raw_to_addrs = str(form_data.get("smtp_to_addrs", "")).strip()
        to_addrs_list = [addr.strip() for addr in raw_to_addrs.split(",") if addr.strip()]
        if not to_addrs_list:
            raise ValueError(translate("err_empty_smtp_to"))

        validated_payload = {
            "language": language,
            "listen_host": listen_host,
            "listen_port": listen_port,
            "log_level": log_level,
            "https_enabled": https_enabled,
            "ssl_certfile": ssl_certfile,
            "ssl_keyfile": ssl_keyfile,
            "db_backend": db_backend,
            "memory_flush_seconds": memory_flush_seconds,
            "retention_days": retention_days,
            "batch_delay_seconds": batch_delay_seconds,
            "page_size": page_size,
            "base_url": base_url,
            "smtp": {
                "host": smtp_host,
                "port": smtp_port,
                "use_ssl": "smtp_use_ssl" in form_data,
                "use_starttls": "smtp_use_starttls" in form_data,
                "verify_ssl": "smtp_verify_ssl" in form_data,
                "user": str(form_data.get("smtp_user", "")).strip(),
                "password": str(form_data.get("smtp_password", "")),
                "from_addr": smtp_from_addr,
                "to_addrs": to_addrs_list,
            },
        }
        update_allowed_settings(validated_payload)
    except ValueError as err:
        logger.warning("Ошибка валидации при сохранении настроек: %s", err)
        return render_template(
            request,
            "settings.html",
            translate("settings_title"),
            user=user,
            cfg=get_editable_settings(),
            available_languages=available_langs,
            msg="",
            error_message=str(err),
        )

    return RedirectResponse("/settings?msg=saved", status_code=303)
