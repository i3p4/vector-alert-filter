# Архитектура проекта Vector Alert Filter

## Карта файлов проекта

### Корневой каталог и конфигурация

* `main.py` — точка входа приложения. Разбирает аргументы командной строки (`--host`, `--port`, `--log-level`), настраивает формат и уровень логирования, управляет жизненным циклом рабочих потоков через `lifespan`, подключает `SessionMiddleware` и запускает сервер `uvicorn`. Открывать при изменении параметров запуска HTTP-сервера или формата логов.
* `requirements.txt` — список зависимостей Python (`fastapi`, `uvicorn`, `python-multipart`, `itsdangerous`). Открывать при добавлении или обновлении внешних библиотек.
* `Dockerfile` — инструкция сборки Docker-образа на базе `python:3.13-slim`. Открывать при изменении системных пакетов, прав пользователя или точки входа контейнера.
* `compose.yml` — конфигурация запуска контейнера в Docker Compose. Открывать при изменении портов, переменных окружения или проброшенных каталогов.
* `vector.toml` — конфигурация агента Vector. Собирает логи из syslog (OpenWrt), Docker-контейнеров и systemd journald, очищает ANSI-цвета, фильтрует сообщения по регулярному выражению ключевых слов ошибок, отсекает дубликаты за 10 секунд (`dedupe_flood`) и отправляет пакеты по HTTP на `/webhook/vector`. Открывать при изменении источников логов или первичного фильтра ключевых слов на стороне Vector.

### Каталог данных (`data/`)

* `data/config.json` — основной файл настроек приложения в формате JSON (параметры сервера, режим БД, тайминги, настройки SMTP и список учетных записей `users`).
* `data/alerts.db` — файл базы данных SQLite (создается автоматически при работе в режиме `sqlite_disk` или при сбросе снимка из памяти в режиме `sqlite_memory`).

### Каталог логики приложения (`app/`)

* `app/config.py` — загрузка `data/config.json`, геттеры параметров (`get_listen_host`, `get_page_size`, `get_smtp_config` и др.), генерация секрета сессии (`get_session_secret`), поиск пользователей при входе (`find_user_by_credentials`, `find_user_by_api_key`), а также потокобезопасное обновление разрешенных настроек (`update_allowed_settings`) с защитой секции `users`. Открывать при добавлении новых параметров в `config.json`.
* `app/utils.py` — функции очистки текста (`decolorize`, `strip_datetime`, `clean_text_pipeline`), генерации SHA-256 хэша связки (`make_bundle_hash`), проверки шаблонов подстрок вида `{{текст}}` (`is_substring_pattern`, `match_message`), разбора входящего события Vector (`extract_clean_vector_event`) и перевода UTC-времени в локальный пояс (`format_local_datetime`). Открывать при добавлении новых форматов дат для вырезания или изменении логики сравнения сообщений.
* `app/database.py` — фабрика `create_database_instance()`, создающая глобальный объект `db` класса `MemorySqliteDatabase` или `DiskSqliteDatabase` в зависимости от параметра `db_backend`. Открывать при добавлении нового типа хранилища.
* `app/db_base.py` — абстрактный базовый класс `BaseAlertDatabase` со схемой таблиц `bundles` и `events`, автоматической очисткой устаревших записей (`_cleanup_old_records`), поиском совпадений (`_find_matching_bundle`), обработкой нового события (`process_incoming_event`) и SQL-запросами для веб-интерфейса (`fetch_bundles`, `create_manual_bundle`, `update_full_bundle`, `delete_bundles`). Открывать при изменении структуры таблиц или логики дедупликации и фильтрации в БД.
* `app/db_sqlite_disk.py` — реализация `DiskSqliteDatabase`, открывающая прямое соединение с `data/alerts.db` в режиме WAL на каждую операцию чтения или записи.
* `app/db_sqlite_memory.py` — реализация `MemorySqliteDatabase`, загружающая базу в `:memory:` при старте и периодически сбрасывающая снимок на диск через `backup()` при наличии флага `_is_dirty`.
* `app/worker.py` — управление очередями `bg_queue` и `mail_queue` и фоновым потоком `BackgroundWorker` (`_bg_worker_loop`). Гарантирует последовательный однопоточный доступ к SQLite как для фоновых пакетов Vector (`_handle_vector_batch`), так и для синхронных задач веб-интерфейса (`submit_ui_task`, `_handle_ui_task`).
* `app/mailer.py` — фоновый поток `MailerWorker` (`_mailer_worker_loop`), накапливающий события в течение `batch_delay_seconds`, группирующий их по каждому `host` (`build_and_send_alerts_email`, `_send_host_email`) и отправляющий HTML и текстовые письма через SMTP (`send_smtp`). Открывать при изменении темы, верстки письма или логики группировки уведомлений.
* `app/web.py` — все HTTP-маршруты FastAPI, HTML-шаблоны страниц, пагинация (`_build_pagination_html`), обработка форм фильтрации, массовых действий (`/bulk-action`), редактирования связок и страницы настроек (`/settings`). Открывать при изменении интерфейса или добавлении новых веб-страниц.

## Устройство сборки и контейнеризации

### Пошаговая сборка образа в Dockerfile

1. Базовый образ: используется официальный облегченный образ `python:3.13-slim`.
2. Аргументы и переменные окружения сборки: задаются `WORK_UID` (`1000`), `WORK_GID` (`1000`), имя пользователя и группы `afilter`, рабочий каталог `/afilter` и оболочка `/bin/bash`. Переменная `HOME` устанавливается в `/afilter`.
3. Метаданные: добавляются метки `org.i3p4.title="i3p4/vaf"` и `org.i3p4.version="1.1"`.
4. Создание непривилегированного пользователя: выполняется `groupadd` и `useradd` для создания пользователя `afilter` с домашней директорией `/afilter`.
5. Установка зависимостей: файл `requirements.txt` копируется в `/afilter/requirements.txt`, затем обновляется `pip` и устанавливаются пакеты без сохранения кэша (`pip install --no-cache -r /afilter/requirements.txt`).
6. Копирование кода: каталоги `app/`, `data/` и файл `main.py` копируются внутрь `/afilter/`.
7. Настройка прав: файлу `/afilter/main.py` выдаются права на исполнение (`chmod ugo+x`), а владельцем всего каталога `/afilter` назначается `afilter:afilter`.
8. Запуск: рабочей директорией назначается `/afilter`, контейнер переключается на пользователя `afilter:afilter` и задает точку входа `ENTRYPOINT ["/afilter/main.py"]` (использует шебанг `#!/usr/bin/env python3` из первой строки `main.py`).

### Конфигурация запуска в compose.yml

1. Сервис `vaf`: запускает единственный контейнер с именем `vaf` из образа `i3p4/vaf:latest` с политикой перезапуска `unless-stopped` и отключенным привилегированным режимом (`privileged: false`).
2. Переменные окружения (`environment`): передается `TZ=Asia/Barnaul`, определяющая локальный часовой пояс внутри контейнера.
3. Тома (`volumes`):
   * `/etc/localtime:/etc/localtime:ro` — проброс системного часового пояса хоста только для чтения.
   * `./vaf:/afilter/data` — монтирование каталога хоста поверх `/afilter/data` внутри контейнера, чтобы `config.json` и `alerts.db` сохранялись между перезапусками и пересборками образа.
4. Сетевые порты (`ports`): внешний TCP-порт `8080` хоста пробрасывается на внутренний порт `8080` контейнера, где слушает `uvicorn`.

## Пошаговые сценарии прохождения запросов

### Сценарий 1: Прием входящего вебхука от Vector и отправка письма

1. Агент Vector отправляет HTTP POST-запрос с JSON-массивом или JSON-объектом событий на эндпоинт `/webhook/vector` в файле `app/web.py` (функция `vector_webhook`).
2. Функция `vector_webhook` немедленно передает сырые данные в функцию `submit_vector_payload` из `app/worker.py`, которая кладет задачу `{"source": "vector", "payload": payload}` в очередь `bg_queue` и возвращает Vector ответ `{"status": "accepted"}`.
3. Фоновый поток `BackgroundWorker` в `app/worker.py` (функция `_bg_worker_loop`) извлекает задачу из `bg_queue` и вызывает `_handle_vector_batch`.
4. Для каждого события в пакете вызывается `extract_clean_vector_event` из `app/utils.py`: определяются `host`, тип сервиса (`container`, `unit` или `unknown`) и имя программы, из строк удаляются ANSI-цвета (`decolorize`) и даты со временем (`strip_datetime`), после чего вычисляется SHA-256 хэш `bundle_hash` (`make_bundle_hash`).
5. Очищенный словарь передается в `db.process_incoming_event` в `app/db_base.py`. Внутри транзакции записи сначала удаляются старые записи старше `retention_days` (`_cleanup_old_records`), затем ищется существующая связка по точному хэшу или по шаблону подстроки `{{...}}` (`_find_matching_bundle`).
6. Если связка найдена, обновляется поле `last_triggered`; если не найдена — создается новая запись в таблице `bundles`. В обоих случаях добавляется запись в таблицу `events`.
7. Если у связки флаг `is_blocked` равен `1`, событие отбрасывается. Если связка активна, словарь события добавляется в список `alerts_to_send` и передается в очередь `mail_queue`.
8. Фоновый поток `MailerWorker` в `app/mailer.py` (функция `_mailer_worker_loop`) получает первую пачку из `mail_queue` и в течение `batch_delay_seconds` дочитывает все последующие пачки, накапливая их в `combined_alerts`.
9. По истечении таймера вызывается `build_and_send_alerts_email` (`app/mailer.py`), которая разделяет события по уникальным значениям `host` и для каждого хоста вызывает `_send_host_email`.
10. Функция `_send_host_email` формирует тему `Сбой: host: prog1, prog2. (N)`, группирует сообщения по `(service, program)` со спойлерами `<details>` и ссылками быстрой блокировки `/api/block`, добавляет подвал с текущим годом и отправляет письмо через `send_smtp`.

### Сценарий 2: Запрос к веб-интерфейсу и работа с базой данных

1. Пользователь открывает страницу в браузере (например, `GET /` в `app/web.py`, функция `index_page`).
2. Функция `get_current_user` проверяет подписанную cookie-сессию `request.session.get("user")` и сверяет логин через `find_user_by_username` в `app/config.py`. Если сессия отсутствует, выполняется редирект `303` на `/login`.
3. Для получения данных из SQLite обработчик `index_page` вызывает `submit_ui_task` в `app/worker.py`, передавая имя действия (например, `"fetch_bundles"`) и личную временную очередь `reply_q = queue.Queue(maxsize=1)`.
4. Поток `BackgroundWorker` извлекает UI-задачу из `bg_queue`, выполняет `_handle_ui_task` (обращаясь к методам `db` в `app/db_base.py` в том же единственном потоке, который владеет соединением с БД) и кладет результат `("ok", res)` в `reply_q`.
5. Обработчик `index_page` получает результат из `reply_q.get()`, переводит метки времени через `format_local_datetime` (`app/utils.py`), формирует HTML с фильтрами, пагинацией и подвалом через `render_page` и возвращает `HTMLResponse`.

## Описание всех маршрутов и контрактов

### Входящие HTTP-эндпоинты приложения (FastAPI)

* `POST /webhook/vector` — прием логов от агента Vector.
  * Заголовки: `Content-Type: application/json` (авторизация не требуется).
  * Входные данные (JSON-объект или JSON-массив объектов): поля `host` (строка), `message` (строка), опционально `container` (строка), `unit` (строка) или `program` (строка).
  * Выходные данные (JSON): `{"status": "accepted"}`.
  * Коды ответов: `200 OK` при постановке пакета в очередь, `422 Unprocessable Entity` при невалидном JSON.

* `GET /login` — отображение HTML-формы входа в веб-интерфейс.
  * Параметры запроса (Query): опционально `error` (строка с текстом ошибки).
  * Выходные данные: HTML-страница (`text/html`).
  * Коды ответов: `200 OK`.

* `POST /login` — проверка учетных данных и создание сессии.
  * Данные формы (`application/x-www-form-urlencoded`): `username` (строка, обязательное), `password` (строка, обязательное).
  * Выходные данные: редирект на `/` с установкой подписанной cookie сессии или повторный рендер `/login` с текстом ошибки.
  * Коды ответов: `303 See Other` при успешном входе, `200 OK` при неверном логине или пароле.

* `GET /logout` — завершение сессии пользователя.
  * Выходные данные: очистка `request.session` и редирект на `/login`.
  * Коды ответов: `303 See Other`.

* `GET /api/block` — быстрая блокировка связки по ссылке из электронного письма (автоматически авторизует сессию по ключу).
  * Параметры запроса (Query): `key` (строка, `api_key` пользователя, обязательный), `host` (строка, обязательный), `service` (строка, по умолчанию `"unknown"`), `program` (строка, обязательный), `message` (строка, обязательный).
  * Выходные данные: редирект на `/bundle/{bundle_id}?msg=blocked`.
  * Коды ответов: `303 See Other` при успехе, `403 Forbidden` при неверном `key`.

* `GET /` — главная страница со списком связок, фильтрами и пагинацией (требуется сессия).
  * Параметры запроса (Query): `only_blocked` (целое число `0` или `1`), `period` (строка из `PERIOD_MAP`, по умолчанию `"1h"`), `filter_host` (строка), `filter_unit` (строка), `filter_container` (строка), `search_msg` (строка), `page` (целое число `>= 1`, по умолчанию `1`).
  * Выходные данные: HTML-страница со списком связок или редирект на `/login`.
  * Коды ответов: `200 OK`, `303 See Other`.

* `POST /bulk-action` — массовая блокировка, разблокировка или удаление выбранных связок (требуется сессия).
  * Данные формы (`application/x-www-form-urlencoded`): `action` (строка: `"block"`, `"unblock"` или `"delete"`), `bundle_ids` (список целых чисел ID связок), скрытые поля состояния фильтров и страницы (`period`, `filter_host`, `filter_unit`, `filter_container`, `search_msg`, `only_blocked`, `page`).
  * Выходные данные: редирект обратно на `/` с сохранением текущих параметров фильтрации и страницы.
  * Коды ответов: `303 See Other`.

* `GET /bundle/new` — HTML-форма ручного создания новой связки или правила подстроки `{{...}}` (требуется сессия).
  * Выходные данные: HTML-страница (`text/html`) или редирект на `/login`.
  * Коды ответов: `200 OK`, `303 See Other`.

* `POST /bundle/new` — сохранение новой ручной связки в БД (требуется сессия).
  * Данные формы (`application/x-www-form-urlencoded`): `host` (строка), `service` (строка: `"container"`, `"unit"`, `"unknown"`), `program` (строка), `message` (строка), `description` (строка), `is_blocked` (целое число `0` или `1`).
  * Выходные данные: редирект на `/bundle/{bundle_id}?msg=saved`.
  * Коды ответов: `303 See Other`.

* `GET /bundle/{bundle_id}` — просмотр статистики и форма редактирования конкретной связки (требуется сессия).
  * Параметры пути (Path): `bundle_id` (целое число). Параметры запроса (Query): `msg` (строка: `"blocked"` или `"saved"`).
  * Выходные данные: HTML-страница или редирект на `/login`.
  * Коды ответов: `200 OK`, `303 See Other`, `404 Not Found` если связка не существует.

* `POST /bundle/{bundle_id}/update` — сохранение изменений или удаление связки (требуется сессия).
  * Параметры пути (Path): `bundle_id` (целое число). Данные формы: `action` (`"save_all"` или `"delete"`), `host`, `service`, `program`, `message`, `description`, `is_blocked`.
  * Выходные данные: редирект на `/` при удалении или на `/bundle/{bundle_id}?msg=saved` при сохранении.
  * Коды ответов: `303 See Other`.

* `GET /settings` — отображение формы разрешенных настроек приложения (требуется сессия).
  * Параметры запроса (Query): `msg` (строка, `"saved"` при успешном сохранении).
  * Выходные данные: HTML-страница с формой настроек или редирект на `/login`.
  * Коды ответов: `200 OK`, `303 See Other`.

* `POST /settings` — валидация и атомарное сохранение разрешенных настроек в `data/config.json` (требуется сессия).
  * Данные формы (`application/x-www-form-urlencoded`): только поля из белого списка `ALLOWED_SETTINGS_FORM_FIELDS` (`listen_host`, `listen_port`, `log_level`, `db_backend`, `memory_flush_seconds`, `retention_days`, `batch_delay_seconds`, `page_size`, `base_url`, `smtp_host`, `smtp_port`, `smtp_use_ssl`, `smtp_use_starttls`, `smtp_verify_ssl`, `smtp_user`, `smtp_password`, `smtp_from_addr`, `smtp_to_addrs`).
  * Выходные данные: редирект на `/settings?msg=saved` при успехе или повторный рендер формы с текстом ошибки валидации.
  * Коды ответов: `303 See Other`, `200 OK` при ошибке валидации значения, `400 Bad Request` при попытке передать поля `users`, `username`, `api_key` или любые неизвестные поля.

### Исходящие запросы (SMTP)

* Исходящее SMTP-соединение (`app/mailer.py`, функция `send_smtp`):
  * Протокол и транспорт: `smtplib.SMTP_SSL` (при `use_ssl=true`) или `smtplib.SMTP` с опциональным `starttls()` (при `use_starttls=true`) на адрес `smtp.host` и порт `smtp.port` с таймаутом 15 секунд.
  * Заголовки письма (`MIMEMultipart("alternative")`): `Subject` (`Сбой: {host}: {programs}. ({count})`), `From` (`smtp.from_addr`), `To` (список `smtp.to_addrs` через запятую), `Date` (локальное время с часовым поясом), `Message-ID`.
  * Тело письма: две части в кодировке UTF-8 — `text/plain` и `text/html` со списком сгруппированных событий, ссылками на `/api/block` и подвалом `© VAF {год}`.
