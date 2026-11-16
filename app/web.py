import logging
from datetime import timedelta
from html import escape
from typing import Any, List, Optional

from fastapi import APIRouter, Body, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import (
    find_user_by_api_key,
    find_user_by_credentials,
    find_user_by_username,
    get_retention_days,
)
from app.utils import is_substring_pattern
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
            f'<span>Пользователь: {escape(user["username"])}</span> | '
            '<a href="/logout">Выйти</a>'
            "</nav><hr>"
        )

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


@router.get("/", response_class=HTMLResponse)
def index_page(
    request: Request,
    only_blocked: int = Query(0),
    period: str = Query("1h"),
    filter_host: str = Query(""),
    filter_unit: str = Query(""),
    filter_container: str = Query(""),
    search_msg: str = Query(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if period not in PERIOD_MAP:
        period = "1h"

    since_delta = PERIOD_MAP[period][1]

    filter_opts = submit_ui_task("fetch_filter_options")
    rows = submit_ui_task(
        "fetch_bundles",
        only_blocked=(only_blocked == 1),
        since_delta=since_delta,
        filter_host=filter_host.strip(),
        filter_unit=filter_unit.strip(),
        filter_container=filter_container.strip(),
        search_msg=search_msg.strip(),
    )

    period_options = []
    for k, (label, _) in PERIOD_MAP.items():
        sel = "selected" if k == period else ""
        period_options.append(f'<option value="{k}" {sel}>{escape(label)}</option>')

    host_options = _build_select_options(filter_opts.get("hosts", []), filter_host, "Все host")
    unit_options = _build_select_options(filter_opts.get("units", []), filter_unit, "Все unit")
    container_options = _build_select_options(
        filter_opts.get("containers", []), filter_container, "Все container"
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
        </div><br>
        <div>
            <label for="search_msg">Поиск по message:</label>
            <input type="text" id="search_msg" name="search_msg" value="{escape(search_msg)}" size="30">

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

    table_rows = []
    for r in rows:
        status_text = "Заблокировано" if r["is_blocked"] else "Активно"
        desc_short = r["description"] if r["description"] else r["message"][:80]
        last_dt = r["last_triggered"][:19].replace("T", " ")

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

    bulk_form = f"""
    <form method="post" action="/bulk-action">
        <div>
            <button type="submit" name="action" value="block">Заблокировать выбранные</button>
            <button type="submit" name="action" value="unblock">Разблокировать выбранные</button>
            <button type="submit" name="action" value="delete">Удалить выбранные из базы</button>
        </div><br>
        <table border="1">
            <caption>Найденные связки событий ({len(rows)})</caption>
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
        </table>
    </form>
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
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if action in ("block", "unblock", "delete"):
        submit_ui_task("bulk_action", bulk_type=action, bundle_ids=bundle_ids)

    return RedirectResponse("/", status_code=303)

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
        <dd>{escape(row['last_triggered'])}</dd>

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
