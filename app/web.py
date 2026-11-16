import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Body, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    find_user_by_api_key,
    find_user_by_credentials,
    find_user_by_username,
    get_editable_settings,
    get_page_size,
    get_retention_days,
    update_allowed_settings,
)
from app.utils import format_local_datetime, is_substring_pattern
from app.worker import submit_ui_task, submit_vector_payload

logger = logging.getLogger("alert_filter.web")
router = APIRouter()

PERIOD_MAP = {
    "1h": ("За последний 1 час", timedelta(hours=1)),
    "3h": ("За последние 3 часа", timedelta(hours=3)),
    "6h": ("За последние 6 часов", timedelta(hours=6)),
    "12h": ("За последние 12 часов", timedelta(hours=12)),
    "1d": ("За 1 день", timedelta(days=1)),
    "3d": ("За 3 дня", timedelta(days=3)),
    "7d": ("За 7 дней", timedelta(days=7)),
    "15d": ("За 15 дней", timedelta(days=15)),
    "30d": ("За 1 месяц", timedelta(days=30)),
    "all": ("Показать все связки", None),
}

ALLOWED_SETTINGS_FORM_FIELDS = frozenset({
    "listen_host",
    "listen_port",
    "log_level",
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
    username = request.session.get("user")
    if not username:
        return None
    return find_user_by_username(username)


def render_page(title: str, content: str, user: Optional[dict] = None) -> HTMLResponse:
    warning_html = ""
    if user and not user.get("api_key"):
        warning_html = (
            '<div role="alert" style="border: 2px solid red; padding: 10px; margin-bottom: 15px;">'
            "<strong>Внимание:</strong> В файле настроек для вашего пользователя не задан уникальный ключ (api_key). "
            "Ссылки быстрой блокировки в письмах работать не будут."
            "</div>"
        )

    nav_html = ""
    if user:
        nav_html = (
            '<nav aria-label="Основная навигация">'
            '<a href="/">Главная (список связок)</a> | '
            '<a href="/bundle/new">Создать новую связку вручную</a> | '
            '<a href="/settings">Настройки</a> | '
            f'<span>Пользователь: {escape(user["username"])}</span> | '
            '<a href="/logout">Выйти</a>'
            "</nav><hr>"
        )

    current_year = datetime.now().year
    full_html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)} — Фильтр алертов Vector</title>
</head>
<body>
    {warning_html}
    {nav_html}
    <main>
        <h1>{escape(title)}</h1>
        {content}
    </main>
    <footer>
        <hr>
        <p>&copy; VAF {current_year}</p>
    </footer>
</body>
</html>"""
    return HTMLResponse(full_html)

@router.get("/login", response_class=HTMLResponse)
def login_page(error: str = ""):
    err_block = f'<p role="alert">{escape(error)}</p>' if error else ""
    html = f"""
    {err_block}
    <form method="post" action="/login">
        <div>
            <label for="username">Логин:</label><br>
            <input type="text" id="username" name="username" required autocomplete="username">
        </div><br>
        <div>
            <label for="password">Пароль:</label><br>
            <input type="password" id="password" name="password" required autocomplete="current-password">
        </div><br>
        <button type="submit">Войти в систему</button>
    </form>
    """
    return render_page("Вход в систему", html)


@router.post("/login")
def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    user = find_user_by_credentials(username, password)
    if user:
        request.session["user"] = user["username"]
        logger.debug("Пользователь %s успешно вошел в веб-интерфейс.", username)
        return RedirectResponse("/", status_code=303)
    logger.debug("Неудачная попытка входа для пользователя %s.", username)
    return login_page("Неверный логин или пароль.")


@router.get("/logout")
def logout_action(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.post("/webhook/vector")
def vector_webhook(payload: Any = Body(...)):
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
    valid_user = find_user_by_api_key(key)
    if not valid_user:
        logger.error("Попытка вызова API блокировки с неверным ключом.")
        raise HTTPException(status_code=403, detail="Неверный или отсутствующий ключ API")

    request.session["user"] = valid_user["username"]

    bundle_id = submit_ui_task(
        "api_block",
        host=host,
        service=service,
        program=program,
        message=message,
    )
    return RedirectResponse(f"/bundle/{bundle_id}?msg=blocked", status_code=303)


def _build_select_options(values: List[str], selected_val: str, empty_label: str) -> str:
    opts = [f'<option value="">{escape(empty_label)}</option>']
    for v in values:
        sel = "selected" if v == selected_val else ""
        opts.append(f'<option value="{escape(v)}" {sel}>{escape(v)}</option>')
    return "".join(opts)


def _build_page_url(
    target_page: int,
    period: str,
    filter_host: str,
    filter_unit: str,
    filter_container: str,
    search_msg: str,
    only_blocked: int,
) -> str:
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

def _build_pagination_html(
    current_page: int,
    total_pages: int,
    period: str,
    filter_host: str,
    filter_unit: str,
    filter_container: str,
    search_msg: str,
    only_blocked: int,
) -> str:
    if total_pages <= 1:
        return ""

    def page_href(p: int) -> str:
        return escape(
            _build_page_url(
                p, period, filter_host, filter_unit, filter_container, search_msg, only_blocked
            )
        )

    parts = ['<nav aria-label="Постраничная навигация">']

    if current_page <= 1:
        parts.append('<span aria-label="Первая страница" aria-disabled="true">&lt;&lt;</span> ')
    else:
        parts.append(f'<a href="{page_href(1)}" aria-label="Первая страница">&lt;&lt;</a> ')

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

    page_tokens = []
    for item in visible_pages:
        if item is None:
            page_tokens.append("...")
        elif item == current_page:
            page_tokens.append(f'<span aria-current="page"><strong>{item}</strong></span>')
        else:
            page_tokens.append(f'<a href="{page_href(item)}" aria-label="Страница {item}">{item}</a>')

    parts.append(", ".join(page_tokens))

    if current_page >= total_pages:
        parts.append(' <span aria-label="Последняя страница" aria-disabled="true">&gt;&gt;</span>')
    else:
        parts.append(f' <a href="{page_href(total_pages)}" aria-label="Последняя страница">&gt;&gt;</a>')

    parts.append("</nav>")
    return "".join(parts)


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
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if period not in PERIOD_MAP:
        period = "1h"

    since_delta = PERIOD_MAP[period][1]
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

    rows = page_result["items"]
    total_count = page_result["total_count"]
    total_pages = page_result["total_pages"]
    current_page = page_result["current_page"]

    period_options = []
    for k, (label, _) in PERIOD_MAP.items():
        sel = "selected" if k == period else ""
        period_options.append(f'<option value="{k}" {sel}>{escape(label)}</option>')

    host_options = _build_select_options(filter_opts.get("hosts", []), clean_host, "Все host")
    unit_options = _build_select_options(filter_opts.get("units", []), clean_unit, "Все unit")
    container_options = _build_select_options(
        filter_opts.get("containers", []), clean_container, "Все container"
    )
    checked_blocked = "checked" if only_blocked == 1 else ""

    filter_form = f"""
    <form method="get" action="/" aria-label="Фильтр и поиск связок">
        <div>
            <label for="period">Период:</label>
            <select id="period" name="period">{''.join(period_options)}</select>

            <label for="filter_host">host:</label>
            <select id="filter_host" name="filter_host">{host_options}</select>

            <label for="filter_unit">unit:</label>
            <select id="filter_unit" name="filter_unit">{unit_options}</select>

            <label for="filter_container">container:</label>
            <select id="filter_container" name="filter_container">{container_options}</select>
        </div>
        <div>
            <label for="search_msg">Поиск по message:</label>
            <input type="text" id="search_msg" name="search_msg" value="{escape(clean_search)}" size="30">

            <input type="checkbox" id="only_blocked" name="only_blocked" value="1" {checked_blocked}>
            <label for="only_blocked">Только заблокированные</label>

            <button type="submit">Применить</button>
            <a href="/">Сбросить</a>
        </div>
    </form>
    <hr>
    """

    if not rows:
        return render_page(
            "Список связок",
            filter_form + "<p>По выбранным условиям записей не найдено.</p>",
            user,
        )

    pagination_html = _build_pagination_html(
        current_page=current_page,
        total_pages=total_pages,
        period=period,
        filter_host=clean_host,
        filter_unit=clean_unit,
        filter_container=clean_container,
        search_msg=clean_search,
        only_blocked=only_blocked,
    )

    table_rows = []
    for r in rows:
        status_text = "Заблокировано" if r["is_blocked"] else "Активно"
        desc_short = r["description"] if r["description"] else r["message"][:80]
        last_dt = format_local_datetime(r["last_triggered"])

        table_rows.append(f"""
        <tr>
            <td>
                <input type="checkbox" class="row-chk" id="chk_{r['id']}" name="bundle_ids" value="{r['id']}">
                <label for="chk_{r['id']}">Выбрать {escape(r['host'])} ({escape(r['service'])}: {escape(r['program'])})</label>
            </td>
            <th scope="row"><a href="/bundle/{r['id']}">{escape(r['host'])}</a></th>
            <td>{escape(r['service'])}</td>
            <td>{escape(r['program'])}</td>
            <td>{escape(desc_short)}</td>
            <td>{escape(last_dt)} (сработок: {r['trigger_count']})</td>
            <td>{status_text}</td>
        </tr>
        """)

    action_buttons_html = """
        <div>
            <button type="submit" name="action" value="block">Заблокировать выбранные</button>
            <button type="submit" name="action" value="unblock">Разблокировать выбранные</button>
            <button type="submit" name="action" value="delete">Удалить выбранные из базы</button>
        </div>
    """

    state_inputs_html = f"""
        <input type="hidden" name="period" value="{escape(period)}">
        <input type="hidden" name="filter_host" value="{escape(clean_host)}">
        <input type="hidden" name="filter_unit" value="{escape(clean_unit)}">
        <input type="hidden" name="filter_container" value="{escape(clean_container)}">
        <input type="hidden" name="search_msg" value="{escape(clean_search)}">
        <input type="hidden" name="only_blocked" value="{1 if only_blocked == 1 else 0}">
        <input type="hidden" name="page" value="{current_page}">
    """

    bulk_form = f"""
    {pagination_html}
    <form method="post" action="/bulk-action">
        {state_inputs_html}
        {action_buttons_html}
        <h2>Найденные связки событий ({total_count})</h2>
        <table border="1">
            <thead>
                <tr>
                    <th scope="col">
                        <input type="checkbox" id="select_all">
                        <label for="select_all">Выбрать все</label>
                    </th>
                    <th scope="col">host</th>
                    <th scope="col">Тип (container / unit)</th>
                    <th scope="col">Программа</th>
                    <th scope="col">Описание</th>
                    <th scope="col">Последняя сработка</th>
                    <th scope="col">Статус</th>
                </tr>
            </thead>
            <tbody>
                {''.join(table_rows)}
            </tbody>
        </table><br>
        {action_buttons_html}
    </form>
    {pagination_html}
    <script>
        (function() {{
            const selectAll = document.getElementById('select_all');
            const rowChks = document.querySelectorAll('.row-chk');
            if (!selectAll) return;
            selectAll.addEventListener('change', function() {{
                rowChks.forEach(function(cb) {{ cb.checked = selectAll.checked; }});
            }});
            rowChks.forEach(function(cb) {{
                cb.addEventListener('change', function() {{
                    const allChecked = Array.from(rowChks).every(function(c) {{ return c.checked; }});
                    selectAll.checked = allChecked;
                }});
            }});
        }})();
    </script>
    """
    return render_page("Список связок", filter_form + bulk_form, user)

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
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if action in ("block", "unblock", "delete"):
        submit_ui_task("bulk_action", bulk_type=action, bundle_ids=bundle_ids)

    safe_period = period if period in PERIOD_MAP else "1h"
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
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    html = """
    <p>Для поиска по части строки заключите текст message в двойные фигурные скобки, например: <code>{{Connection refused}}</code>.</p>
    <form method="post" action="/bundle/new">
        <div>
            <label for="host">host:</label><br>
            <input type="text" id="host" name="host" required size="50">
        </div><br>
        <div>
            <label for="service">Тип сервиса (container / unit):</label><br>
            <select id="service" name="service">
                <option value="container">container</option>
                <option value="unit">unit</option>
                <option value="unknown">unknown</option>
            </select>
        </div><br>
        <div>
            <label for="program">Имя container или unit:</label><br>
            <input type="text" id="program" name="program" required size="50">
        </div><br>
        <div>
            <label for="message">message (или {{часть строки}}):</label><br>
            <textarea id="message" name="message" rows="3" cols="60" required></textarea>
        </div><br>
        <div>
            <label for="description">Описание:</label><br>
            <textarea id="description" name="description" rows="3" cols="60"></textarea>
        </div><br>
        <div>
            <input type="checkbox" id="is_blocked" name="is_blocked" value="1" checked>
            <label for="is_blocked">Сразу заблокировать эту связку</label>
        </div><br>
        <button type="submit">Создать связку</button>
    </form>
    """
    return render_page("Создание новой связки вручную", html, user)


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
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    row = submit_ui_task("fetch_bundle_by_id", bundle_id=bundle_id)
    if not row:
        raise HTTPException(status_code=404, detail="Связка не найдена")

    alert_banner = ""
    if msg == "blocked":
        alert_banner = '<p role="alert"><strong>Статус обновлен:</strong> Связка заблокирована. Уведомления по ней приходить не будут.</p>'
    elif msg == "saved":
        alert_banner = '<p role="alert"><strong>Сохранено:</strong> Изменения применены.</p>'

    status_text = (
        "Заблокирована (уведомления отключены)"
        if row["is_blocked"]
        else "Активна (уведомления отправляются)"
    )
    match_mode = (
        "По вхождению подстроки ({{...}})"
        if is_substring_pattern(row["message"])
        else "Полное совпадение очищенной строки"
    )

    sel_container = "selected" if row["service"] == "container" else ""
    sel_unit = "selected" if row["service"] == "unit" else ""
    sel_unknown = "selected" if row["service"] not in ("container", "unit") else ""
    chk_blocked = "checked" if row["is_blocked"] else ""

    html = f"""
    {alert_banner}
    <dl>
        <dt><strong>Режим сравнения message:</strong></dt>
        <dd>{match_mode}</dd>

        <dt><strong>Последняя сработка:</strong></dt>
        <dd>{escape(format_local_datetime(row['last_triggered']))}</dd>

        <dt><strong>Количество срабатываний за период хранения ({get_retention_days()} дн.):</strong></dt>
        <dd>{row['trigger_count']}</dd>

        <dt><strong>Текущий статус:</strong></dt>
        <dd>{status_text}</dd>
    </dl>

    <hr>
    <h2>Полное редактирование связки</h2>
    <p>Подсказка: чтобы проверять только наличие части текста в логе, оберните ее в двойные фигурные скобки: <code>{{часть текста}}</code>.</p>
    <form method="post" action="/bundle/{bundle_id}/update">
        <div>
            <label for="host">host:</label><br>
            <input type="text" id="host" name="host" value="{escape(row['host'])}" required size="50">
        </div><br>
        <div>
            <label for="service">Тип сервиса (container / unit):</label><br>
            <select id="service" name="service">
                <option value="container" {sel_container}>container</option>
                <option value="unit" {sel_unit}>unit</option>
                <option value="unknown" {sel_unknown}>unknown</option>
            </select>
        </div><br>
        <div>
            <label for="program">Имя container или unit:</label><br>
            <input type="text" id="program" name="program" value="{escape(row['program'])}" required size="50">
        </div><br>
        <div>
            <label for="message">message (для сравнения связки):</label><br>
            <textarea id="message" name="message" rows="3" cols="60" required>{escape(row['message'])}</textarea>
        </div><br>
        <div>
            <label for="description">Описание (для отображения):</label><br>
            <textarea id="description" name="description" rows="4" cols="60">{escape(row['description'])}</textarea>
        </div><br>
        <div>
            <input type="checkbox" id="is_blocked" name="is_blocked" value="1" {chk_blocked}>
            <label for="is_blocked">Заблокировать уведомления по этой связке</label>
        </div><br>
        <button type="submit" name="action" value="save_all">Сохранить все изменения</button>
        <button type="submit" name="action" value="delete">Полностью удалить связку из базы</button>
    </form>
    """
    return render_page(f"Связка #{bundle_id}: {row['host']}", html, user)


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

def _render_settings_form(
    user: dict,
    cfg: dict,
    banner_html: str = "",
) -> HTMLResponse:
    sel_debug = "selected" if cfg["log_level"] == "DEBUG" else ""
    sel_info = "selected" if cfg["log_level"] == "INFO" else ""
    sel_error = "selected" if cfg["log_level"] == "ERROR" else ""

    sel_disk = "selected" if cfg["db_backend"] == "sqlite_disk" else ""
    sel_mem = "selected" if cfg["db_backend"] == "sqlite_memory" else ""

    chk_ssl = "checked" if cfg["smtp_use_ssl"] else ""
    chk_starttls = "checked" if cfg["smtp_use_starttls"] else ""
    chk_verify = "checked" if cfg["smtp_verify_ssl"] else ""

    html = f"""
    {banner_html}
    <form method="post" action="/settings">
        <fieldset>
            <legend>Веб-сервер и логирование</legend>
            <div>
                <label for="listen_host">IP-адрес сервера:</label><br>
                <input type="text" id="listen_host" name="listen_host" value="{escape(str(cfg['listen_host']))}" required size="30" aria-describedby="desc_listen_host">
                <p id="desc_listen_host"><small>Сетевой адрес, на котором веб-интерфейс принимает подключения (0.0.0.0 — все интерфейсы). Применяется после перезапуска службы.</small></p>
            </div>
            <div>
                <label for="listen_port">Порт сервера:</label><br>
                <input type="number" id="listen_port" name="listen_port" value="{int(cfg['listen_port'])}" min="1" max="65535" required aria-describedby="desc_listen_port">
                <p id="desc_listen_port"><small>TCP-порт для доступа к веб-интерфейсу и приема событий от Vector. Применяется после перезапуска службы.</small></p>
            </div>
            <div>
                <label for="base_url">Внешний адрес (URL):</label><br>
                <input type="text" id="base_url" name="base_url" value="{escape(str(cfg['base_url']))}" size="50" aria-describedby="desc_base_url">
                <p id="desc_base_url"><small>Базовый адрес приложения для формирования ссылок быстрой блокировки в письмах (например, http://nas.lan:8080).</small></p>
            </div>
            <div>
                <label for="log_level">Уровень логирования:</label><br>
                <select id="log_level" name="log_level" aria-describedby="desc_log_level">
                    <option value="INFO" {sel_info}>INFO (обычный)</option>
                    <option value="DEBUG" {sel_debug}>DEBUG (подробный)</option>
                    <option value="ERROR" {sel_error}>ERROR (только ошибки)</option>
                </select>
                <p id="desc_log_level"><small>Подробность системного журнала приложения при запуске службы.</small></p>
            </div>
        </fieldset><br>

        <fieldset>
            <legend>База данных и интерфейс</legend>
            <div>
                <label for="db_backend">Режим хранения базы:</label><br>
                <select id="db_backend" name="db_backend" aria-describedby="desc_db_backend">
                    <option value="sqlite_memory" {sel_mem}>В оперативной памяти (sqlite_memory)</option>
                    <option value="sqlite_disk" {sel_disk}>На диске (sqlite_disk)</option>
                </select>
                <p id="desc_db_backend"><small>Режим работы SQLite: в памяти снижает обращения к дискам NAS, на диске сохраняет каждое событие сразу. Применяется после перезапуска службы.</small></p>
            </div>
            <div>
                <label for="memory_flush_seconds">Интервал сброса на диск (сек.):</label><br>
                <input type="number" id="memory_flush_seconds" name="memory_flush_seconds" value="{int(cfg['memory_flush_seconds'])}" min="10" required aria-describedby="desc_memory_flush">
                <p id="desc_memory_flush"><small>Как часто сохранять базу из оперативной памяти на диск в режиме sqlite_memory (если были изменения).</small></p>
            </div>
            <div>
                <label for="retention_days">Срок хранения истории (дней):</label><br>
                <input type="number" id="retention_days" name="retention_days" value="{int(cfg['retention_days'])}" min="1" required aria-describedby="desc_retention_days">
                <p id="desc_retention_days"><small>Количество дней, в течение которых хранятся записи сработок и неактивные связки до автоматического удаления.</small></p>
            </div>
            <div>
                <label for="page_size">Связок на странице:</label><br>
                <input type="number" id="page_size" name="page_size" value="{int(cfg['page_size'])}" min="1" max="1000" required aria-describedby="desc_page_size">
                <p id="desc_page_size"><small>Максимальное количество связок, выводимых на одной странице списка на главной странице.</small></p>
            </div>
        </fieldset><br>

        <fieldset>
            <legend>Почта и уведомления (SMTP)</legend>
            <div>
                <label for="batch_delay_seconds">Задержка группировки писем (сек.):</label><br>
                <input type="number" id="batch_delay_seconds" name="batch_delay_seconds" value="{float(cfg['batch_delay_seconds'])}" min="0" step="1" required aria-describedby="desc_batch_delay">
                <p id="desc_batch_delay"><small>Время ожидания перед отправкой письма для объединения нескольких сбоев одного хоста в одну сводку (0 — отправлять сразу).</small></p>
            </div>
            <div>
                <label for="smtp_host">SMTP-сервер:</label><br>
                <input type="text" id="smtp_host" name="smtp_host" value="{escape(str(cfg['smtp_host']))}" required size="40" aria-describedby="desc_smtp_host">
                <p id="desc_smtp_host"><small>Адрес почтового сервера для отправки писем с уведомлениями.</small></p>
            </div>
            <div>
                <label for="smtp_port">SMTP-порт:</label><br>
                <input type="number" id="smtp_port" name="smtp_port" value="{int(cfg['smtp_port'])}" min="1" max="65535" required aria-describedby="desc_smtp_port">
                <p id="desc_smtp_port"><small>Порт почтового сервера (обычно 465 для SSL или 587 для STARTTLS).</small></p>
            </div>
            <div>
                <input type="checkbox" id="smtp_use_ssl" name="smtp_use_ssl" value="1" {chk_ssl} aria-describedby="desc_smtp_ssl">
                <label for="smtp_use_ssl">Использовать SSL</label>
                <p id="desc_smtp_ssl"><small>Шифрованное соединение с почтовым сервером с момента подключения (порт 465).</small></p>
            </div>
            <div>
                <input type="checkbox" id="smtp_use_starttls" name="smtp_use_starttls" value="1" {chk_starttls} aria-describedby="desc_smtp_starttls">
                <label for="smtp_use_starttls">Использовать STARTTLS</label>
                <p id="desc_smtp_starttls"><small>Переход на защищенное соединение после установки обычного подключения (обычно порт 587).</small></p>
            </div>
            <div>
                <input type="checkbox" id="smtp_verify_ssl" name="smtp_verify_ssl" value="1" {chk_verify} aria-describedby="desc_smtp_verify">
                <label for="smtp_verify_ssl">Проверять SSL-сертификат</label>
                <p id="desc_smtp_verify"><small>Проверка подлинности сертификата почтового сервера (отключите для самоподписанных сертификатов локального NAS).</small></p>
            </div>
            <div>
                <label for="smtp_user">Логин SMTP:</label><br>
                <input type="text" id="smtp_user" name="smtp_user" value="{escape(str(cfg['smtp_user']))}" size="40" autocomplete="off" aria-describedby="desc_smtp_user">
                <p id="desc_smtp_user"><small>Имя учетной записи для авторизации на почтовом сервере (оставьте пустым, если авторизация не требуется).</small></p>
            </div>
            <div>
                <label for="smtp_password">Новый пароль SMTP:</label><br>
                <input type="password" id="smtp_password" name="smtp_password" value="" size="40" autocomplete="new-password" aria-describedby="desc_smtp_password">
                <p id="desc_smtp_password"><small>Оставьте поле пустым, чтобы сохранить текущий пароль от почтового ящика без изменений.</small></p>
            </div>
            <div>
                <label for="smtp_from_addr">Адрес отправителя:</label><br>
                <input type="email" id="smtp_from_addr" name="smtp_from_addr" value="{escape(str(cfg['smtp_from_addr']))}" required size="40" aria-describedby="desc_smtp_from">
                <p id="desc_smtp_from"><small>Email-адрес, который будет указан в поле «От кого» в отправляемых уведомлениях.</small></p>
            </div>
            <div>
                <label for="smtp_to_addrs">Адреса получателей:</label><br>
                <input type="text" id="smtp_to_addrs" name="smtp_to_addrs" value="{escape(str(cfg['smtp_to_addrs']))}" required size="50" aria-describedby="desc_smtp_to">
                <p id="desc_smtp_to"><small>Один или несколько email-адресов получателей уведомлений, перечисленных через запятую.</small></p>
            </div>
        </fieldset><br>

        <button type="submit">Сохранить настройки</button>
    </form>
    """
    return render_page("Настройки приложения", html, user)

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    banner = ""
    if msg == "saved":
        banner = '<p role="alert"><strong>Сохранено:</strong> Настройки успешно записаны в файл конфигурации.</p>'

    return _render_settings_form(user, get_editable_settings(), banner)


@router.post("/settings", response_class=HTMLResponse)
async def settings_save(request: Request):
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
            detail="Запрос отклонен: переданы запрещенные или недопустимые параметры конфигурации.",
        )

    try:
        listen_host = str(form_data.get("listen_host", "")).strip()
        if not listen_host:
            raise ValueError("IP-адрес сервера не может быть пустым.")

        listen_port = int(str(form_data.get("listen_port", "")).strip())
        if not (1 <= listen_port <= 65535):
            raise ValueError("Порт сервера должен быть в диапазоне от 1 до 65535.")

        log_level = str(form_data.get("log_level", "INFO")).strip().upper()
        if log_level not in ("DEBUG", "INFO", "ERROR"):
            raise ValueError("Недопустимый уровень логирования.")

        db_backend = str(form_data.get("db_backend", "sqlite_disk")).strip().lower()
        if db_backend not in ("sqlite_disk", "sqlite_memory"):
            raise ValueError("Недопустимый режим хранения базы данных.")

        memory_flush_seconds = int(str(form_data.get("memory_flush_seconds", "")).strip())
        if memory_flush_seconds < 10:
            raise ValueError("Интервал сброса на диск должен быть не менее 10 секунд.")

        retention_days = int(str(form_data.get("retention_days", "")).strip())
        if retention_days < 1:
            raise ValueError("Срок хранения истории должен быть не менее 1 дня.")

        batch_delay_seconds = float(str(form_data.get("batch_delay_seconds", "")).strip())
        if batch_delay_seconds < 0:
            raise ValueError("Задержка группировки писем не может быть отрицательной.")

        page_size = int(str(form_data.get("page_size", "")).strip())
        if page_size < 1:
            raise ValueError("Количество связок на странице должно быть не менее 1.")

        base_url = str(form_data.get("base_url", "")).strip().rstrip("/")

        smtp_host = str(form_data.get("smtp_host", "")).strip()
        if not smtp_host:
            raise ValueError("Адрес SMTP-сервера не может быть пустым.")

        smtp_port = int(str(form_data.get("smtp_port", "")).strip())
        if not (1 <= smtp_port <= 65535):
            raise ValueError("Порт SMTP-сервера должен быть в диапазоне от 1 до 65535.")

        smtp_from_addr = str(form_data.get("smtp_from_addr", "")).strip()
        if not smtp_from_addr:
            raise ValueError("Адрес отправителя SMTP не может быть пустым.")

        raw_to_addrs = str(form_data.get("smtp_to_addrs", "")).strip()
        to_addrs_list = [addr.strip() for addr in raw_to_addrs.split(",") if addr.strip()]
        if not to_addrs_list:
            raise ValueError("Укажите хотя бы один адрес получателя уведомлений.")

        validated_payload = {
            "listen_host": listen_host,
            "listen_port": listen_port,
            "log_level": log_level,
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
        err_banner = f'<p role="alert"><strong>Ошибка:</strong> {escape(str(err))}</p>'
        return _render_settings_form(user, get_editable_settings(), err_banner)

    return RedirectResponse("/settings?msg=saved", status_code=303)
