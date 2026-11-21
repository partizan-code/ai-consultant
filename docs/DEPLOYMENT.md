# Первое размещение на VPS

[Главная](../README.md) · [Настройка](SETUP.md) · [Ограничения](ROADMAP.md)

Цель: Ubuntu 24.04 x86_64, существующий Nginx, домен `ai.exprofit.by`.
Это инструкция и шаблоны: установка на сервер и переключение рабочего канала
ещё не выполнялись. Существующие сайты, приложения и порт `8000` не изменяются.
Порты ниже свободны по первоначальному обследованию; проверить повторно перед запуском.

## 1. Разделение окружений

| | production | staging |
| --- | --- | --- |
| Пользователь | `ai-consultant-production` | `ai-consultant-staging` |
| Каталог релизов | `/srv/ai-consultant/production/releases/` | `/srv/ai-consultant/staging/releases/` |
| Активный релиз | `/srv/ai-consultant/production/current` | `/srv/ai-consultant/staging/current` |
| Конфигурация | `/etc/ai-consultant/production.env` | `/etc/ai-consultant/staging.env` |
| Данные | `/var/lib/ai-consultant/production/` | `/var/lib/ai-consultant/staging/` |
| Backend | `127.0.0.1:8100` | `127.0.0.1:8101` |
| Панель | `127.0.0.1:8501` | `127.0.0.1:8502` |
| Доступ | HTTPS + Basic Auth | SSH-туннель |

Релизы, включая `.venv`, подготавливает `korobey`; сервисные пользователи только
читают код и пишут в собственный каталог данных. Файлы `.env` доступны только
соответствующему сервисному пользователю и root, режим `0600`. Данные — `0700`.
Пользователи staging и production разные: staging не должен читать рабочую базу
или секреты production. Не использовать домашний каталог `korobey` для кода
и интерпретатора сервисов: systemd закрывает `/home` через `ProtectHome=true`.

После проверки отсутствия одноимённых пользователей и каталогов создать их
адресно; команды не выполняют повторно вслепую:

```bash
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/ai-consultant-production --shell /usr/sbin/nologin ai-consultant-production
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/ai-consultant-staging --shell /usr/sbin/nologin ai-consultant-staging
sudo install -d -o korobey -g korobey -m 0755 /srv/ai-consultant /srv/ai-consultant/tools /srv/ai-consultant/production/releases /srv/ai-consultant/staging/releases
sudo install -d -o root -g root -m 0755 /etc/ai-consultant /var/lib/ai-consultant
sudo install -d -o ai-consultant-production -g ai-consultant-production -m 0700 /var/lib/ai-consultant/production
sudo install -d -o ai-consultant-staging -g ai-consultant-staging -m 0700 /var/lib/ai-consultant/staging
sudo install -d -o root -g root -m 0700 /var/backups/ai-consultant
```

## 2. Инструменты и выбранный коммит

Git, uv и certbot обнаружены на VPS, mise пока не найден в PATH. Установку mise
выполнить отдельно по [официальной инструкции](https://mise.jdx.dev/getting-started.html).
Существующие системные Python и инструменты других приложений не заменять.

Для установки Python использовать отдельное хранилище mise, доступное сервисам:

```bash
export MISE_DATA_DIR=/srv/ai-consultant/tools/mise
mise install python@3.13
```

Эту переменную сохранять для последующих команд подготовки релизов. Проверить,
что сервисные пользователи могут исполнять установленный Python; не менять
права чужих каталогов. `.venv` создаётся только через uv. Каждый релиз каждого
окружения имеет собственную `.venv`, пересобирать её после переноса каталога.

После локальных автономных тестов и отдельно разрешённых commit/push получить
репозиторий в отдельный служебный checkout. Зафиксировать полный SHA релиза
из GitHub. Не разрабатывать в production и не делать там `git pull`.
Команды ниже выполняются из чистого служебного checkout, с проверенным SHA:

```bash
release_commit=FULL_COMMIT_SHA
release_env=staging
release_dir=/srv/ai-consultant/$release_env/releases/$release_commit
git cat-file -e "$release_commit^{commit}"
test ! -e "$release_dir" && mkdir "$release_dir"
```

Продолжать только если обе проверки успешны; при ошибке остановиться. Затем:

```bash
git archive "$release_commit" | tar -x -C "$release_dir"
cd "$release_dir"
mise trust
mise exec -- uv sync --locked --no-dev --no-editable
```

Архив содержит только закоммиченные файлы, без локальных `.env`, баз и `.git`.
`--locked` запрещает менять lockfile, `--no-editable` устанавливает пакет в `.venv`.
В systemd запускается готовая `.venv` без uv/mise, установки пакетов и скачивания.
Для production повторить сборку **того же SHA** в его отдельном каталоге.
Убедиться, что `.venv/bin/python` и пакет доступны нужному сервисному пользователю.

## 3. Конфигурация и systemd

Образцы: [production](../deploy/production.env.example),
[staging](../deploy/staging.env.example). Установить каждый в соответствующий
файл `/etc/ai-consultant/` с владельцем из таблицы и режимом `0600`.
Существующие файлы не перезаписывать. Редактировать через `sudoedit`, секреты
не передавать в аргументах команд и не присылать в чат.

Для staging создать отдельные `ADMIN_TOKEN` и `WEBHOOK_TOKEN`; внешние ключи
оставить пустыми. В `APP_ENV=staging` оба внешних HTTP-клиента заблокированы
на уровне транспорта, обработчик консультаций выключен, регистрации webhook нет
даже при `REGISTER_WEBHOOK=true` и случайно заполненных ключах. Можно принимать
синтетические события и проверять панель; реальная генерация, импорт истории
и отправка недоступны. Подсказки и результаты отправки проверяются автономными
тестами с подменённой сетью. Не копировать рабочие данные для браузерной приёмки.

В production указать собственные токены и данные Umnico/OpenRouter, проверить
`UMNICO_USER_ID`. `REGISTER_WEBHOOK=false` оставить до согласованного переключения.
Этот флаг отключает только регистрацию; он **не запрещает** приём событий,
генерацию и отправку в local/production. До переключения использовать новую пустую
базу, не публиковать маршрут webhook и не отправлять сообщения вручную.

Системные шаблоны:

- [backend](../deploy/systemd/ai-consultant@.service);
- [панель](../deploy/systemd/ai-consultant-panel@.service).

Скопировать их в `/etc/systemd/system/`, не изменяя существующие сервисы.
Проверить units через `systemd-analyze verify` на сервере. Backend запускается
в одном процессе; автоматической миграции существующей схемы нет.

Указать `current` как символьную ссылку на подготовленный релиз соответствующего
окружения. При первом запуске создать её через `ln -s`; при обновлении менять
только проверенную символьную ссылку, без рекурсивного удаления каталогов.
Для первого staging после установки файлов и ссылки:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ai-consultant@staging ai-consultant-panel@staging
curl --fail --silent --show-error http://127.0.0.1:8101/health
sudo systemctl status ai-consultant@staging ai-consultant-panel@staging --no-pager
```

Для панели staging открыть **с локальной машины** туннель:

```bash
ssh -L 18502:127.0.0.1:8502 korobey@SERVER_IP
```

Открыть `http://127.0.0.1:18502`. Nginx не публикует staging. Для синтетических
сообщений использовать формат из `tests/test_web.py`; не использовать реальные
идентификаторы клиентов, рабочий webhook или базу production.

### Необязательный прокси для OpenRouter

Если OpenRouter недоступен напрямую с VPS, сначала проверить доступ через отдельно
настроенный локальный HTTP-прокси. Например, для прокси на `127.0.0.1:18080`:

```bash
curl --proxy http://127.0.0.1:18080 --noproxy "" --max-time 30 \
  --silent --show-error --fail --output /dev/null --write-out 'HTTP: %{http_code}\n' \
  https://openrouter.ai/api/v1/models
```

Это публичный список моделей без API-ключа и генерации; HTTP 200 подтверждает только
доступность этого адреса через прокси. Затем в `/etc/ai-consultant/production.env` задать:

```dotenv
OPENROUTER_PROXY_URL=http://127.0.0.1:18080
```

Параметр поддерживается релизами с этой настройкой; в старом релизе одно добавление
строки ничего не меняет. Сначала собрать и проверить новый релиз по процедуре ниже,
затем перезапустить backend. Прокси используется только для OpenRouter; Umnico и
панель сохраняют свои маршруты. При недоступности прокси запрос завершается ошибкой,
без прямого обходного подключения. Общие `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`
игнорируются. Пустое значение настройки восстанавливает прямой доступ после рестарта.
На staging прокси не отменяет запрет внешних запросов.

Прокси должен запускаться независимо от интерактивного SSH-сеанса и восстанавливаться
после перезагрузки; локальный порт не публиковать наружу. Для контейнера использовать
привязку вида `127.0.0.1:18080:8080`. Секреты VPN и URL прокси с паролями хранить
вне Git с ограниченными правами, не выводить в журналы или чат. Настройка прокси
не требует изменения Nginx, DNS или системного маршрута по умолчанию.

## 4. DNS, HTTPS и Nginx

1. Убедиться, что `ai.exprofit.by` разрешается в IPv4 этого VPS. Если есть AAAA,
   она тоже должна вести на этот VPS с доступным IPv6, иначе сначала исправить DNS.
2. Проверить порты и конфликты `server_name`, сохранить копию изменяемой
   конфигурации Nginx. Не заменять `nginx.conf`, `default` и существующие сайты.
3. Создать `/var/www/ai-consultant-acme` и установить только
   [HTTP-шаблон](../deploy/nginx/ai.exprofit.by.http.conf) как отдельный сайт
   `ai.exprofit.by`. Он открывает исключительно ACME challenge, панель недоступна.
4. После `sudo nginx -t` выполнить `sudo systemctl reload nginx`.
5. Выпустить отдельный сертификат, без автоматического редактирования чужих сайтов:

   ```bash
   sudo certbot certonly --webroot -w /var/www/ai-consultant-acme -d ai.exprofit.by
   ```

6. Подготовить `/etc/nginx/ai-consultant.htpasswd`: отдельная учётная запись
   администратора, файл root:www-data, режим `0640`. Пароль вводить интерактивно
   через `htpasswd`, без параметра `-b`; `-c` использовать только для нового файла.
   Проверить наличие утилиты, при отсутствии отдельно установить `apache2-utils`.
7. После приёмки staging, запуска production и готовности к переключению заменить
   **только новый сайт** на [HTTPS-шаблон](../deploy/nginx/ai.exprofit.by.conf).
   Сначала `nginx -t`, затем reload; при ошибке reload не выполнять.
8. Проверить продление сертификата и существующий механизм reload Nginx после
   продления. Если reload не настроен, добавить отдельный deploy-hook с
   `nginx -t && systemctl reload nginx`; существующие hooks не заменять.
   `certbot renew --cert-name ai.exprofit.by --dry-run` проверяет только этот сертификат.

Панель закрыта Basic Auth поверх HTTPS; WebSocket передаётся через Nginx,
защиты CORS/XSRF Streamlit остаются включёнными. Backend и Streamlit слушают
только loopback, `/admin` и `/health` публично закрыты. Маршрут webhook доступен
без Basic Auth, но проверяет собственный секретный токен в приложении.

Чтобы секретный URL не попадал в журналы, у нового виртуального хоста отключён
access-log, error-log направлен в `/dev/null`; HTTP не перенаправляет секретный путь.
Приложение отключает Uvicorn access-log. Диагностика — через безопасные сообщения
backend в journal и локальный `/health`. Не включать отладочные логи запросов,
не выводить реальные URL webhook и секреты в терминал или чат.

Основания конфигурации: [WebSocket Nginx](https://nginx.org/en/docs/http/websocket.html),
[журналы Nginx](https://nginx.org/en/docs/http/ngx_http_log_module.html),
[изоляция systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html),
[настройки Streamlit](https://docs.streamlit.io/develop/api-reference/configuration/config.toml).

## 5. Данные, переключение и откат

До первого production решить, нужна ли локальная история. По умолчанию использовать
новую базу. Перенос существующей базы, миграцию и регистрацию рабочего webhook
выполнять отдельными согласованными действиями.

Для резервной копии использовать SQLite backup API либо останавливать **оба**
сервиса окружения до копирования. Не копировать активный `.sqlite3` отдельно
от возможных WAL/SHM. Копии с перепиской и контактами хранить в
`/var/backups/ai-consultant/` под root с режимом `0600`; настроить регулярное
копирование и хранение за пределами VPS. Восстановить копию в отдельном временном
каталоге, проверить `PRAGMA integrity_check` и чтение основных таблиц, без внешних
ключей и запуска production. Существующую схему обновлять только по
[процедуре миграции](MIGRATION_SUGGESTIONS.md), с остановкой, бэкапом и планом отката.

Переключение рабочего канала:

1. Принять staging, записать проверенный SHA. Проверить качество базы знаний.
2. Подготовить тот же SHA для production, базу и секреты; проверить loopback health,
   защищённый вход, WebSocket и сохранение состояния при перезапуске.
3. Остановить локальный backend и убедиться, что его автозапуск не вернёт старую
   регистрацию. Проверить старые webhook tuna в Umnico и отключить/удалить только
   относящиеся к этому приложению; не удалять чужие регистрации.
4. Открыть production-маршрут, установить `REGISTER_WEBHOOK=true`, перезапустить
   backend и проверить регистрацию. Не допускать одновременно два обработчика канала.
5. Проверить согласованный тестовый Direct, повторное событие, восстановление
   после рестарта и результат отправки. Новые чаты по-прежнему **на паузе**;
   автоматическая активация в этот этап не входит.

Для обновления собрать новый каталог, остановить оба сервиса окружения, сделать
бэкап и переключить `current` на проверенный SHA, затем запустить оба сервиса.
Проверить `/health`, journal, панель и версию ссылки. Не менять код в текущем релизе.
Для отката остановить оба сервиса и вернуть ссылку на предыдущий совместимый релиз.
Если менялась схема, сначала выполнить отдельно согласованное восстановление
совместимой копии базы; потерю событий после бэкапа оценить до восстановления.
Старые каталоги сохранять до приёмки. Автоматического деплоя и миграций пока нет.

Готовность VPS, units и Nginx проверяется на сервере отдельно. Локальные тесты
подтверждают поведение приложения, но не заменяют `nginx -t`, проверку systemd,
сертификата, бэкапов, браузера и тестового канала.
