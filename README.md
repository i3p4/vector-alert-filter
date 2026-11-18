# Vector Alert Filter (VAF)

**Language:** **English** | [Русский](README.ru.md)

## Service Overview

The Vector Alert Filter (VAF) microservice receives log batches from a Vector agent over an HTTP/HTTPS webhook, strips ANSI color codes and date/time stamps from messages, groups identical errors into bundles, and stores trigger history in an SQLite database. If a bundle is not blocked by the user, the service groups events by host and sends a consolidated HTML and plain-text email digest via SMTP.

A ready-to-use Vector agent configuration example (`vector.toml`) is located in the repository root. It configures log collection (from syslog, Docker, and systemd journald), initial error filtering, and formatting events into the JSON structure expected by the microservice (`host`, `container` or `unit`, `message`).

## Important Notes

### Security Limitations and Scope

This application is designed for a home server (NAS) and a trusted local network. The microservice is not intended for public internet exposure or critical production environments, as it uses a simplified security model (no brute-force protection, CSRF tokens, or role-based access control, and the `/webhook/vector` log ingestion endpoint is unauthenticated).

### Accessibility First Principle

The web interface and HTML email structure are designed from the ground up with maximum usability for blind users and screen readers (NVDA, JAWS, VoiceOver) as a top priority. Strict semantic markup, logical heading hierarchy, clear labels for all fields and links, and the absence of visual clutter remain mandatory requirements for any future updates.

## Web Interface and Notification Features

* **Main Bundles List Page:** View all recorded errors along with trigger counts and the timestamp of the last event in the container's local time zone.
* **Flexible Filtering and Search:** Filter bundles by time period (from 1 hour to 30 days or all time), hostname, source type and name (`unit` or `container`), message substring, and block status.
* **Pagination:** Compact page navigation at both the top and bottom of the list while preserving all active filters.
* **Bulk and Single Actions:** Block, unblock, and delete selected bundles directly from the list page (returning to the current page with filters intact) or from an individual bundle detail page.
* **Flexible Blocking Rules:** Manually create a bundle or edit an existing one using double curly braces `{{text fragment}}` to block all logs containing the specified substring.
* **Smart Email Notifications:** Group failures over a configurable delay interval and send a separate email per affected host, featuring a descriptive subject line with affected programs, collapsible lists for repeated messages, and one-click direct links to block a specific bundle straight from the email.
* **Localization (i18n) and Templates:** Full support for Russian and English interfaces and emails, with easy addition of new languages by placing JSON dictionaries in `app/locales/`, and clean separation of logic from Jinja2 templates (`app/templates/`).
* **Built-in HTTPS Support:** Enable encrypted connections directly in settings by specifying certificate and private key filenames located inside the `data/` directory.
* **Settings Page (`/settings`):** Configure server, language, HTTPS, database, retention period, page size, and SMTP parameters directly from the browser with localized labels and descriptions for every field. To protect credentials, editing users, passwords, and API keys via the web interface is completely disabled.

## Environment Variables and Configuration

Environment variables are configured in the `environment` section of `compose.yml` (or via an `.env` file):

* `TZ` (string, set to `Asia/Krasnoyarsk` in the project) — sets the time zone inside the container for accurate local timestamps in the web interface, email headers, and application logs.

Additionally, the system time file and configuration directory are mounted into the container:

* `/etc/localtime:/etc/localtime:ro` — synchronizes the container's system time with the host machine in read-only mode.
* `./vector-alert-filter:/afilter/data` — mounts persistent storage on the host for the `config.json` settings file, SSL certificates, and the `alerts.db` database file.

## Overriding Startup Parameters via `command` in `compose.yml`

The container entrypoint `ENTRYPOINT ["/afilter/main.py"]` accepts optional command-line arguments that take precedence over values in `data/config.json`. Exactly three arguments are supported in `main.py`:

* `--host` (string) — network interface IP address to listen on (overrides `listen_host` in `config.json`).
* `--port` (integer) — TCP port for the web server and webhook (overrides `listen_port` in `config.json`).
* `--log-level` (string: `DEBUG`, `INFO`, or `ERROR`) — system log verbosity level (overrides `log_level` in `config.json`).

Example of using the `command` section with all available parameters in `compose.yml`:

```yaml
services:
  vector-alert-filter:
    container_name: vector-alert-filter
    image: i3p4/vector-alert-filter:latest
    environment:
      - TZ=Asia/Krasnoyarsk
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ./vector-alert-filter:/afilter/data
    restart: unless-stopped
    ports:
      - 8080:8080
    privileged: false
    command: ["--host", "0.0.0.0", "--port", "8080", "--log-level", "DEBUG"]
```

## Container Management Commands

All commands are executed from the project directory containing `compose.yml`.

1. Local image build without starting the container (after modifying Python code in `app/`, `main.py`, or dependencies in `requirements.txt`):

```bash
docker build -t i3p4/vector-alert-filter:latest .
```

2. Start the container in detached mode without building:

```bash
docker compose up -d
```

3. Force recreate the container after modifying `compose.yml`, the `command` section, or environment variables:

```bash
docker compose up -d --force-recreate
```

4. View live service logs (press `Ctrl+C` to exit):

```bash
docker compose logs -f vector-alert-filter
```

5. View the last 100 lines of logs:

```bash
docker compose logs --tail=100 vector-alert-filter
```

6. Restart the service (required after changing `listen_host`, `listen_port`, `log_level`, `db_backend`, `https_enabled`, `ssl_certfile`, or `ssl_keyfile` via the web interface):

```bash
docker compose restart vector-alert-filter
```

7. Stop and remove the container (data in `./vector-alert-filter` is preserved):

```bash
docker compose down
```

8. Open an interactive shell inside the running container (type `exit` to leave without stopping the service):

```bash
docker compose exec -it vector-alert-filter /bin/bash
```

9. Attach directly to the main running process standard input/output (press `Ctrl+P` followed by `Ctrl+Q` to detach safely without stopping the container; `Ctrl+C` will stop the container):

```bash
docker attach vector-alert-filter
```
