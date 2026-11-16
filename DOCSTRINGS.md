# === Для вставки в app/utils.py ===

def clean_text_pipeline(raw_text: str) -> str:
    """Очищает строку лога от ANSI-цветов и меток даты и времени.

    Args:
        raw_text: Исходная строка сообщения или имени сервиса из Vector.

    Returns:
        Нормализованная строка без цветовых последовательностей и временных меток.
    """

def make_bundle_hash(host: str, service: str, program: str, message: str) -> str:
    """Вычисляет детерминированный SHA-256 хэш связки событий в нижнем регистре.

    Args:
        host: Имя хоста-источника.
        service: Тип источника ('container', 'unit' или 'unknown').
        program: Имя контейнера, юнита или программы.
        message: Очищенный текст сообщения.

    Returns:
        Шестнадцатеричная строка SHA-256 хэша длиной 64 символа.
    """

def extract_clean_vector_event(event: dict) -> dict:
    """Извлекает и нормализует поля одиночного события из пакета Vector.

    Args:
        event: Сырой словарь события, полученный от Vector по вебхуку.

    Returns:
        Словарь с очищенными ключами 'host', 'service', 'program', 'message' и 'bundle_hash'.
    """

def format_local_datetime(iso_str: str) -> str:
    """Переводит ISO-метку времени из UTC в локальный часовой пояс контейнера.

    Args:
        iso_str: Строка даты и времени в формате ISO 8601 (предпочтительно с часовым поясом).

    Returns:
        Строка локального времени в формате 'YYYY-MM-DD HH:MM:SS'.
    """

# === Для вставки в app/config.py ===

def update_allowed_settings(validated_data: dict[str, Any]):
    """Атомарно обновляет разрешенные настройки в памяти и в файле data/config.json.

    Блокирует любые попытки изменения секции 'users' и учетных данных пользователей.
    Пустое значение 'smtp.password' игнорируется для сохранения текущего пароля.

    Args:
        validated_data: Словарь проверенных параметров из белого списка ALLOWED_TOP_LEVEL_KEYS.

    Raises:
        ValueError: Если передан запрещенный или неизвестный ключ конфигурации.
    """

# === Для вставки в app/db_base.py ===

def process_incoming_event(self, clean_data: dict) -> Optional[dict]:
    """Регистрирует входящее событие в БД и определяет необходимость уведомления.

    Выполняет очистку устаревших записей, ищет связку по точному хэшу или шаблону
    подстроки '{{...}}', обновляет счетчик сработок и возвращает данные для отправки,
    если связка не заблокирована.

    Args:
        clean_data: Нормализованный словарь события из extract_clean_vector_event.

    Returns:
        Словарь с данными связки для отправки на почту или None, если связка заблокирована.
    """

def fetch_bundles(
    self,
    only_blocked: bool,
    since_delta: Optional[timedelta],
    filter_host: str = "",
    filter_unit: str = "",
    filter_container: str = "",
    search_msg: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict:
    """Возвращает отфильтрованную страницу связок с подсчетом общего количества записей.

    Args:
        only_blocked: Если True, выбирать только заблокированные связки.
        since_delta: Временной интервал от текущего момента UTC или None для выборки за все время.
        filter_host: Фильтр по точному совпадению имени хоста.
        filter_unit: Фильтр по имени systemd-юнита.
        filter_container: Фильтр по имени Docker-контейнера.
        search_msg: Подстрока для регистронезависимого поиска по полю message.
        page: Номер запрашиваемой страницы (начиная с 1).
        page_size: Количество записей на одной странице.

    Returns:
        Словарь с ключами 'items' (список связок), 'total_count', 'total_pages' и 'current_page'.
    """

# === Для вставки в app/worker.py ===

def submit_vector_payload(payload: Any):
    """Ставит входящий пакет событий от Vector в фоновую очередь обработки.

    Args:
        payload: Разобранный JSON-объект или список словарей событий от Vector.
    """

def submit_ui_task(action: str, **kwargs) -> Any:
    """Синхронно выполняет задачу веб-интерфейса в потоке владельца БД через очередь.

    Создает временную очередь ответа reply_q и блокирует вызывающий поток FastAPI
    до получения результата или исключения из BackgroundWorker.

    Args:
        action: Имя операции БД ('fetch_bundles', 'bulk_action', 'create_manual' и др.).
        **kwargs: Именованные аргументы для выбранного действия.

    Returns:
        Результат выполнения запрошенной операции из слоя базы данных.

    Raises:
        RuntimeError: Если сервис находится в процессе остановки и очередь закрыта.
        Exception: Любое исключение, возникшее при выполнении запроса в БД.
    """

def _handle_vector_batch(payload: Any):
    """Разбирает пакет событий Vector, сохраняет их в БД и передает активные в почтовик.

    Args:
        payload: Словарь одиночного события или список словарей из входящего вебхука.
    """

# === Для вставки в app/mailer.py ===

def build_and_send_alerts_email(alerts_to_send: list):
    """Группирует накопленные события по хостам и отправляет отдельное письмо на каждый хост.

    Args:
        alerts_to_send: Список словарей активных (незаблокированных) событий для отправки.
    """

def _send_host_email(host: str, host_alerts: list, base_url: str, api_key: str):
    """Формирует HTML и текстовое письмо для одного хоста и передает его в SMTP.

    Args:
        host: Имя хоста, для которого формируется сводка сбоев.
        host_alerts: Список событий, относящихся к указанному хосту.
        base_url: Базовый URL веб-интерфейса для генерации ссылок быстрой блокировки.
        api_key: API-ключ пользователя для авторизации ссылок /api/block.
    """

def send_smtp(subject: str, html_body: str, text_body: str):
    """Отправляет multipart-письмо (plain + html) через настроенный SMTP-сервер.

    Args:
        subject: Тема письма.
        html_body: HTML-версия тела письма в кодировке UTF-8.
        text_body: Текстовая версия тела письма в кодировке UTF-8.
    """

# === Для вставки в app/web.py ===

def render_page(title: str, content: str, user: Optional[dict] = None) -> HTMLResponse:
    """Оборачивает контент страницы в единый доступный HTML-каркас с навигацией и подвалом.

    Args:
        title: Заголовок страницы (выводится в тегах title и h1).
        content: Основной HTML-контент для вставки внутрь тега main.
        user: Словарь текущего авторизованного пользователя или None для страницы входа.

    Returns:
        Готовый объект HTMLResponse со сформированной разметкой.
    """

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
    """Генерирует доступную компактную навигацию по страницам вида '<< 1, 2 ... N >>'.

    Возвращает пустую строку, если общее количество страниц не превышает 1.

    Args:
        current_page: Номер текущей отображаемой страницы.
        total_pages: Общее количество страниц по текущим фильтрам.
        period: Выбранный код временного периода из PERIOD_MAP.
        filter_host: Текущий фильтр по имени хоста.
        filter_unit: Текущий фильтр по имени systemd-юнита.
        filter_container: Текущий фильтр по имени Docker-контейнера.
        search_msg: Текущая строка поиска по тексту сообщения.
        only_blocked: Флаг отображения только заблокированных связок (1 или 0).

    Returns:
        HTML-строка с тегом nav или пустая строка при единственной странице.
    """

async def settings_save(request: Request):
    """Валидирует и сохраняет разрешенные настройки из формы POST /settings.

    Отклоняет запрос с кодом 400 при наличии запрещенных полей ('users', 'username',
    'api_key') или неизвестных параметров формы.

    Args:
        request: Входящий HTTP-запрос FastAPI с данными формы.

    Returns:
        RedirectResponse на страницу настроек при успехе или HTMLResponse с ошибкой.

    Raises:
        HTTPException: С кодом 400 при попытке передать недопустимые поля конфигурации.
    """
