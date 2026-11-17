# FinanceIQ

FinanceIQ — персональная веб-платформа для учета, анализа и прогнозирования финансов. Проект помогает понимать структуру доходов и расходов, отслеживать денежные потоки и прогнозировать расчетный остаток по проведенным операциям на ближайшие 30 дней.

Разрабатывается как практический инструмент для личных финансов и портфолио-проект с акцентом на точность расчетов, безопасность пользовательских данных и поддерживаемую архитектуру.

## Текущий статус

Этап 0 завершен 2026-11-02: согласованы продуктовые правила финансового фундамента, доменная модель, PostgreSQL-схема, матрица инвариантов и архитектура MVP (пакеты 0.13/0.14). По отдельным поручениям пользователя 2026-11-04 выполнены задачи 1.1–1.4: создан устанавливаемый каркас backend, закреплены версии Python и инструментов, настроены форматирование, lint, строгая проверка типов и конфигурация через переменные окружения с явным подключением `.env`, добавлено минимальное FastAPI-приложение с проверкой работоспособности. Установка, проверки и запуск HTTP-сервера выполнены на Windows x64 и Linux x86_64 с CPython 3.14.10; подробности — в ROADMAP. Остальные задачи этапа 1 выполняются по отдельным поручениям; этап 1 не завершен. В задаче 1.5 от 2026-11-05 добавлены asyncpg/SQLAlchemy, сессии на запрос, сервисные транзакции и безопасные DB-ошибки. Чистая установка и 59 unit-тестов проверены на Windows/Linux; PostgreSQL не запускалась по согласованной границе приемки. В 1.8 от 2026-11-17 добавлены pytest, изолированная тестовая PostgreSQL и реальные проверки runtime-транзакций, отложенного FK, денежной точности и восстановления соединений. В 1.6 от 2026-11-16 добавлены Alembic и отдельные роли миграций/runtime, проверенные на одноразовой PostgreSQL 18.7. В 1.7 от 2026-11-16 добавлено локальное окружение Docker Compose с отдельным migrator, файловыми secrets и проверками готовности. Предметные таблицы еще не реализованы. Детали Auth D-12 и прогноза D-11 остаются на своих контрольных точках.

## Планируемые возможности MVP

- Регистрация и авторизация с изоляцией данных пользователей.
- Управление финансовыми счетами, просмотр и фильтрация транзакций.
- Импорт ручного журнала Money Manager XLSX с проверкой, предварительным просмотром, выявлением дублей и подтверждением.
- Автоматическая категоризация по правилам и ручное изменение категорий.
- Дашборд доходов, расходов, денежных потоков и динамики по периодам.
- Базовый прогноз расчетного остатка на 30 дней при достаточной истории и подтвержденных остатках счетов, с указанием ограничений расчета.

MVP рассчитан на одну валюту — RUB — и один профиль импорта: `money_manager_xlsx_v1` из ручного журнала Money Manager (согласован 2026-10-30 вместо банковского CSV). Задачи проектирования 0.7–0.12 завершены: профиль, идентичность книги, fingerprint, явное разрешение повторов, блокирующие ошибки и допустимые действия, лимиты ресурсов, срок preview, хранение, жизненный цикл фактов и детали категорий согласованы. Исходный журнал ведется в Money Manager; FinanceIQ допускает исправление классификации/связей, контролируемую отмену всей книги и архив счета с сохранением истории. Приняты нормализация текста, приоритеты правил, десять персональных начальных категорий без правил и архивирование категорий. В 0.13 модель/схема актуализированы: подготовлены попытки/наблюдения/отмены, доказательства решений и матрица 36 инвариантов с 12 дополнительными сценариями. Технический пакет 0.13, доменная модель и схема финансового фундамента приняты пользователем 2026-11-02. В 0.14 согласована архитектура: семь модулей, структура monorepo, сервисные транзакции, взаимодействие и механизмы ресурсов/очистки. Пакет 0.14 принят пользователем 2026-11-02; этап 0 завершен. Текущий ход реализации отражен в [ROADMAP.md](docs/ROADMAP.md). Отмена импорта, очистка и резервное копирование еще не реализованы.

## Выбранный стек

- **Frontend:** Next.js (App Router), TypeScript, Tailwind CSS, shadcn/ui, TanStack Query, React Hook Form, Zod, Recharts.
- **Backend:** Python, FastAPI, SQLAlchemy 2, Alembic, Pydantic; Pandas для аналитики.
- **База данных:** PostgreSQL; денежные расчеты — Python Decimal и PostgreSQL NUMERIC.
- **Инфраструктура и тестирование:** Docker Compose, GitHub Actions, pytest, Vitest, Playwright.

Архитектура — монорепозиторий с модульным монолитом на backend и REST API с префиксом `/api/v1`.

## Структура backend

```text
backend/
├── .python-version
├── pyproject.toml
├── alembic.ini
├── migrations/
├── requirements/
│   ├── dev-windows-py314.txt
│   └── dev-linux-py314.txt
├── src/financeiq/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   ├── core/
│   ├── db/
│   ├── shared/
│   └── modules/
│       ├── auth/
│       ├── accounts/
│       ├── transactions/
│       ├── categories/
│       ├── imports/
│       ├── analytics/
│       └── forecasting/
└── tests/
    ├── unit/
    ├── integration/
    └── support/                 # одноразовая PostgreSQL и технические фабрики
```

`main.py` содержит фабрику приложения, `api` — HTTP-границу с общим router `/api/v1` и health endpoint, `core` — конфигурацию и lifecycle, `db` — инфраструктуру БД, `shared` — общие примитивы. Бизнес-модули пока содержат только `__init__.py`; файлы слоев добавляются вместе с соответствующей функциональностью. Alembic и техническая начальная ревизия добавлены в 1.6; в 1.8 подключены pytest и общие технические фабрики/фикстуры.

## Локальная установка backend

Требуется CPython **3.14.10** с модулями `venv` и `pip`; эталонная версия записана в `backend/.python-version`. Метаданные пакета допускают исправления ветки `>=3.14.10,<3.15`; обновление эталонного patch-релиза сопровождается повторной проверкой окружений. Файл `.python-version` сам не устанавливает и не переключает Python.

Используются `venv` и `pip`. В `backend/pyproject.toml` закреплены runtime-зависимости SQLAlchemy с extra `asyncio` 2.0.57, asyncpg 0.32.0, Alembic 1.20.1, FastAPI 0.162.0, Pydantic 2.14.2, pydantic-settings 2.16.2 и базовый Uvicorn 0.54.0, dev-зависимости asyncpg-stubs 0.32.0, HTTPX 0.28.1, pip 26.2.2, pip-tools 7.6.2, Ruff 0.17.3, mypy 2.4.1, pytest 9.2.0, pytest-asyncio 1.5.0 и сборочный backend setuptools 84.2.1. asyncpg-stubs нужен для строгой проверки типов: сам драйвер не поставляет `py.typed`; подавление ошибок mypy не добавляется. Сгенерированные lock-файлы содержат точные версии и SHA-256 всех прямых, транзитивных и сборочных зависимостей. Windows и Linux используют отдельные lock-файлы. Требуется доступ к PyPI либо заранее подготовленный cache.

Команды выполняются из корня репозитория. Если существующий `.venv` создан другой версией Python, пересоздайте его перед установкой. На Windows выберите установленный Python 3.14.10; при использовании launcher проверьте его командой `py -3.14 --version`.

PowerShell:

```powershell
cd backend
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/dev-windows-py314.txt
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe -c "import financeiq; print(financeiq.__file__)"
```

Linux:

```bash
cd backend
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements/dev-linux-py314.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/python -c "import financeiq; print(financeiq.__file__)"
```

Команды используют Python созданного окружения напрямую, поэтому его активация не требуется. Editable-установка делает пакет доступным без изменения `PYTHONPATH`. Все зависимости предварительно устанавливаются из lock-файла; `--no-deps --no-build-isolation` предотвращает отдельное разрешение зависимостей при установке пакета. Инфраструктура SQLAlchemy добавлена в 1.5; миграции и права проверены на PostgreSQL в 1.6. Реальные runtime-проверки добавлены в 1.8; команды приведены в разделе проверок backend.

## Настройки backend

`financeiq.core.config.load_settings(*, env_file: Path | None = None)` возвращает неизменяемый `Settings`. Конфигурация читается при вызове, без глобального экземпляра или cache. Импорт модуля не загружает настройки. Загрузка не меняет переменные процесса и не настраивает logging.

| Переменная окружения | Поле | Допустимые значения | По умолчанию |
| --- | --- | --- | --- |
| `FINANCEIQ_APP_ENV` | `app_env` | `development`, `test`, `production` | `development` |
| `FINANCEIQ_LOG_LEVEL` | `log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `FINANCEIQ_DATABASE_URL` | `database_url` (`SecretStr`) | `postgresql+asyncpg://user:password@host:port/database` | Нет, обязательна |

Имена переменных регистронезависимы; используйте приведенное написание. Значения регистрозависимы: пустая строка, `Production` или `info` являются ошибкой. `development` не включает отладочные endpoints или дополнительные логи. URL должен содержать пользователя, хост и имя базы; порт необязателен, при указании допустим диапазон 1–65535. Неэкранированные пробельные символы запрещены; специальные символы в credentials кодируются через percent-encoding. URL хранится как секрет и исключен из `repr(Settings)`; отсутствие или ошибка URL прерывает создание приложения с безопасным `ConfigurationError`. Полномочия миграций описаны ниже; Auth появится после согласования D-12.

По умолчанию `load_settings()` читает только окружение процесса. Для CLI-запуска Uvicorn сначала задайте `FINANCEIQ_DATABASE_URL` в том же терминале. Синтетический пример, credentials и имя базы нужно заменить:

```powershell
# PowerShell
$env:FINANCEIQ_DATABASE_URL = 'postgresql+asyncpg://financeiq_runtime:replace_me@127.0.0.1:5432/financeiq'
```

```bash
# Linux
export FINANCEIQ_DATABASE_URL='postgresql+asyncpg://financeiq_runtime:replace_me@127.0.0.1:5432/financeiq'
```

Локальный `.env` необязателен. Для его использования выполните из корня репозитория после установки backend:

```powershell
# PowerShell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
.\backend\.venv\Scripts\python.exe -c "from pathlib import Path; from financeiq.core.config import load_settings; load_settings(env_file=Path('.env'))"
```

```bash
# Linux
cp -n .env.example .env
backend/.venv/bin/python -c "from pathlib import Path; from financeiq.core.config import load_settings; load_settings(env_file=Path('.env'))"
```

Команды проверяют конфигурацию без вывода ее значений; успешное завершение имеет код 0. Не копируйте пример поверх существующего локального файла. Переменные процесса имеют приоритет над указанным файлом, затем применяются значения по умолчанию. Файл читается в UTF-8; относительный путь отсчитывается от текущего каталога. Из `backend` тот же корневой файл указывается как `Path('../.env')`. Автоматического поиска в текущем или родительских каталогах нет. Фабрика `create_app` явно вызывает загрузчик при создании приложения.

Отсутствующий, недоступный файл, неверная кодировка, недопустимое значение или неизвестный ключ в файле приводят к `ConfigurationError`. Неизвестные ключи отклоняются и при пустом значении; записи без `=` также отклоняются. Посторонние переменные процесса игнорируются. Ошибка сообщает безопасную причину и известные имена настроек, не раскрывая переданные значения, неизвестные ключи, содержимое файла или исходную цепочку исключений. Для `Settings` отключен вывод исходных значений библиотекой даже при `PYDANTIC_SETTINGS_DEBUG=1`. Не выводите полный объект настроек или исходные ошибки Pydantic в логи.

`.env` и `.env.*` исключены из Git на любой глубине; `.env.example` остается доступным для отслеживания и содержит только несекретные примеры. Настоящие credentials и финансовые данные не добавляются в примеры, документацию или репозиторий.

## Локальный запуск backend

После установки и задания обязательного `FINANCEIQ_DATABASE_URL` выполните из `backend`:

```powershell
# PowerShell
.\.venv\Scripts\python.exe -m uvicorn financeiq.main:create_app --factory --host 127.0.0.1 --port 8000
```

```bash
# Linux
.venv/bin/python -m uvicorn financeiq.main:create_app --factory --host 127.0.0.1 --port 8000
```

В другом терминале проверьте HTTP-приложение:

```powershell
# PowerShell
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/health
```

```bash
# Linux, при наличии curl
curl --fail http://127.0.0.1:8000/api/v1/health
```

Публичный `GET /api/v1/health` возвращает HTTP 200, `Content-Type: application/json` и строго `{"status":"ok"}`. Это проверка ответа HTTP-приложения без обращений к БД, файлам или внешним сервисам; готовность PostgreSQL ею не подтверждается. Финансовые данные, настройки и версии компонентов в ответ не включаются. Маршрут `/health` отсутствует; POST к `/api/v1/health` возвращает 405.

`financeiq.main.create_app(*, env_file: Path | None = None)` создает независимое приложение, один раз загружает настройки и сохраняет их в `app.state.settings`. Импорт модуля не загружает конфигурацию, не создает приложение и не запускает сервер. Фабрика не меняет окружение и конфигурацию logging; `debug=False` во всех режимах. Неверная конфигурация прерывает создание приложения с безопасным `ConfigurationError`.

| Режим `FINANCEIQ_APP_ENV` | Swagger UI | OpenAPI |
| --- | --- | --- |
| `development`, `test` | `/api/v1/docs` | `/api/v1/openapi.json` |
| `production` | Отключен, HTTP 404 | Отключен, HTTP 404 |

ReDoc, OAuth redirect Swagger и непрефиксированные `/docs`, `/redoc`, `/openapi.json` отключены. Выбор режима читается при создании приложения; после изменения окружения перезапустите сервер. Экспорт OpenAPI и генерация TypeScript-контрактов остаются в задаче 10.3.

Для запуска с явно выбранным корневым `.env` используйте из `backend` программную передачу пути фабрике:

```powershell
# PowerShell
.\.venv\Scripts\python.exe -c "from pathlib import Path; import uvicorn; from financeiq.main import create_app; app = create_app(env_file=Path('../.env')); uvicorn.run(app, host='127.0.0.1', port=8000, log_level=app.state.settings.log_level.lower())"
```

```bash
# Linux
.venv/bin/python -c "from pathlib import Path; import uvicorn; from financeiq.main import create_app; app = create_app(env_file=Path('../.env')); uvicorn.run(app, host='127.0.0.1', port=8000, log_level=app.state.settings.log_level.lower())"
```

В программном примере уровень логов Uvicorn берется из `Settings.log_level`. При запуске через CLI уровень задается параметром Uvicorn `--log-level` (по умолчанию `info`); `FINANCEIQ_LOG_LEVEL` самостоятельно logging не настраивает. Остановка сервера — Ctrl+C.

## Локальное окружение Docker Compose

Нужны Docker Engine с Linux-контейнерами, Docker Compose с поддержкой `up --wait` и Python 3.11+ для подготовки секретов. Локальная установка backend и PostgreSQL client tools для этого запуска не требуются. Используются закрепленные по digest Python 3.14.10 и PostgreSQL 18.7. Образ backend предназначен для локальной разработки и устанавливает существующий Linux dev lock с SHA-256; он включает инструменты проверок.

Из корня репозитория:

```powershell
# PowerShell; существующие локальные настройки не перезаписываются.
if (-not (Test-Path -LiteralPath .env.compose)) { Copy-Item .env.compose.example .env.compose }
python infra/compose/setup.py
docker compose --env-file .env.compose config --quiet
docker compose --env-file .env.compose up --build --wait --wait-timeout 180
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

```bash
# Linux
test -e .env.compose || cp .env.compose.example .env.compose
python3 infra/compose/setup.py
docker compose --env-file .env.compose config --quiet
docker compose --env-file .env.compose up --build --wait --wait-timeout 180
curl --fail http://127.0.0.1:8000/api/v1/health
```

`.env.compose` используется только для подстановок Compose: `FINANCEIQ_BACKEND_PORT=8000`, `FINANCEIQ_POSTGRES_PORT=5432`, `FINANCEIQ_SECRETS_DIR=./.secrets/compose`. При занятых портах измените их в этом файле; публикация остается только на `127.0.0.1`. Из контейнеров PostgreSQL доступна как `postgres:5432`, независимо от порта хоста. `.env.compose` не передается загрузчикам приложения или Alembic. Существующие `.env` и `.env.migrations` используются только при отдельном запуске backend/миграций на хосте.

Скрипт создает три разных пароля в `.secrets/compose`: `postgres_password`, `migrator_password`, `runtime_password`. Повторный запуск сохраняет весь существующий комплект; неполный, пустой или недоступный комплект приводит к безопасному отказу. При другом `FINANCEIQ_SECRETS_DIR` укажите тот же каталог через `python infra/compose/setup.py --directory <directory>`. Секреты не выводятся, не попадают в Git или Docker build context. В Linux каталог создается с `0700`, файлы — с `0444`: каталог защищает доступ на хосте, отдельные read-only mounts читаются непривилегированными процессами контейнеров. На Windows доступ на хосте определяется ACL каталога; атрибуты `uid/gid/mode` файловых secrets в Compose не заменяют эти права.

| Сервис | Назначение и доступ |
| --- | --- |
| `postgres` | Администратор `postgres` владеет БД; имеет три файла паролей для первичной настройки |
| `migrate` | Одноразовый `alembic upgrade head`, только секрет `financeiq_migrator` |
| `backend` | Один процесс Uvicorn от UID 10001, только секрет `financeiq_runtime` |

Первый запуск пустого volume выполняет существующий bootstrap и задает пароли через `psql \password`, подавая их в stdin. PostgreSQL использует SCRAM для TCP-подключений. Миграции ожидают успешный TCP `pg_isready`; API запускается после успешных миграций. Контейнерный адаптер `python -m financeiq.core.container` поддерживает `serve`, `migrate [alembic arguments]` и `ready`; читает только секрет своей роли и формирует URL без передачи пароля через аргументы команд. Обычные `Settings` и HTTP-фабрика сохраняют прежние интерфейсы.

Healthcheck backend проверяет HTTP (timeout 2 секунды) и `SELECT 1` через runtime engine (DB timeout 5 секунд), затем освобождает ресурсы. Интервал — 5 секунд, timeout контейнера — 10 секунд, 6 неудач дают `unhealthy`. Публичный `/api/v1/health` остается liveness без обращения к БД: при остановке PostgreSQL он может отвечать `{"status":"ok"}`, хотя контейнер backend уже `unhealthy`. Состояние `unhealthy` само по себе не перезапускает контейнер; после восстановления БД проверка повторяется автоматически.

Основные команды из корня, одинаковые для PowerShell и Linux:

```text
docker compose --env-file .env.compose ps --all
docker compose --env-file .env.compose logs --tail 100
docker compose --env-file .env.compose run --rm migrate migrate current
docker compose --env-file .env.compose run --rm migrate migrate check
docker compose --env-file .env.compose exec -T backend python -m financeiq.core.container ready
docker compose --env-file .env.compose down
```

Изменения исходников применяются повторным `up --build --wait --wait-timeout 180`. Автоперезагрузка и монтирование исходников отключены. Обычный `down` сохраняет именованный volume, смонтированный в `/var/lib/postgresql`; контейнеры не настроены на автоматический запуск после перезапуска Docker. Команда `restart` не заменяет применение миграций через `up`.

### Существующий volume и восстановление

Init-скрипты выполняются только на пустом volume. Повторный запуск не меняет пароли, а изменение файлов secrets не обновляет credentials существующей БД. Неверный пароль migrator останавливает запуск до API; неверный runtime-пароль приводит к `unhealthy` backend.

При утраченном файле сначала восстановите его из защищенной локальной копии. Не генерируйте новый комплект поверх существующей БД. Если пароли намеренно изменены или первичная инициализация прервалась, сохраните доступный комплект и явно повторите административную настройку:

```text
docker compose --env-file .env.compose stop backend
docker compose --env-file .env.compose up -d postgres
docker compose --env-file .env.compose exec -T postgres bash /docker-entrypoint-initdb.d/10-financeiq.sh
docker compose --env-file .env.compose up --build --wait --wait-timeout 180
```

Команда настройки повторяет проверяемый bootstrap и **устанавливает оба пароля прикладных ролей из текущих файлов**, сохраняя данные. Административный пароль PostgreSQL она не меняет. Для его изменения используйте `docker compose --env-file .env.compose exec postgres psql -X -U postgres -d financeiq`, затем интерактивную `\password postgres`; файл `postgres_password` должен соответствовать назначенному паролю. Если bootstrap отвергает несовместимые права или владение, требуется административное исправление согласно разделу миграций. Автоматического удаления volume нет; `down --volumes` удаляет данные и не является способом обычного восстановления. Самостоятельное управление этим локальным volume не заменяет будущие резервные копии.

## Сессии и транзакции БД

FastAPI lifespan создает отдельные `app.state.engine` и `app.state.session_factory` для каждого приложения и освобождает движок через `await engine.dispose()` при остановке. Импорт модулей и вызов `create_app()` не создают движок. Подключение ленивое: первый DB-запрос получает соединение. Корректный URL требуется даже для health, но доступность сервера PostgreSQL при запуске и в health не проверяется.

`db.session.create_engine(settings)` использует asyncpg, `READ COMMITTED`, `pool_pre_ping=True`, таймаут подключения 5 секунд и стандартный пул SQLAlchemy. Pre-ping заменяет закрытые соединения в пуле; разрыв активной транзакции завершает команду ошибкой без автоматического повтора. Обработчик DB-ошибок сохраняет распознанные SQLAlchemy исключения драйвера и признаки disconnect до безопасной сервисной/HTTP-границы. `create_session_factory(engine)` задает `autobegin=False`, `autoflush=False`, `expire_on_commit=False`. В `api.dependencies` доступны `get_session(request)` и аннотация `DatabaseSession` с `Depends(..., scope="function")`: одна сессия кешируется в рамках запроса и закрывается до отправки ответа. Одновременным запросам или async-задачам нужны отдельные сессии. Для служебного сценария без FastAPI используется `session_scope(factory)`.

Внешний сервис сценария открывает `async with transaction(session)`. Выход без ошибки выполняет один commit, исключение — rollback; после завершения можно открыть следующую транзакцию той же сессии. Внутренние сервисы получают ту же сессию и не делают commit; repository может делать flush. Зависимость только выдает/закрывает сессию и никогда не фиксирует изменения. Незавершенная транзакция откатывается при закрытии. Очистка дожидается rollback/close/dispose даже при повторной отмене задачи; отмена передается дальше, включая случай ошибки очистки. Сбой commit не считается успехом и не вызывает автоматического повтора команды. Потеря связи во время COMMIT может оставлять исход неизвестным; правила восстановления конкретных финансовых команд реализуются вместе с ними.

DB-граница возвращает безопасный `DatabaseError` без исходной цепочки исключений. Необработанная ошибка дает HTTP 500 и строго `{"detail":"Database operation failed."}`; в технический лог попадает только `database_error`. Отключены `echo` и `echo_pool`, включен `hide_parameters`; собственные уникальные логгеры движка/пула подавлены даже при DEBUG предка, root logger не меняется. Ошибки подключения и неизвестные ошибки драйвера при SQL-выполнении санитизируются отдельно: они не всегда являются `SQLAlchemyError`. Доменные ошибки и общий HTTP-контракт остаются на этапе 3. Новых HTTP endpoints для проверки БД нет.

## Миграции и полномочия PostgreSQL

Задача 1.6 использует PostgreSQL 18.7 и защищенную схему `public`. Alembic запускается отдельным процессом из checkout; файлы `alembic.ini` и `migrations/` не входят в Python wheel. HTTP-приложение не запускает миграции и не получает их credentials.

Первичная настройка выполняется администратором на отдельной локальной БД `financeiq`. Требуются `createdb` и `psql` из PostgreSQL client tools; пароль администратора вводится интерактивно. Следующие команды одинаковы для PowerShell и Linux и выполняются из корня репозитория; замените `postgres`, host и port при необходимости:

```text
createdb -h 127.0.0.1 -p 5432 -U postgres --owner=postgres financeiq
psql -X -h 127.0.0.1 -p 5432 -U postgres -d financeiq -v ON_ERROR_STOP=1 -f infra/postgres/bootstrap.sql
psql -X -h 127.0.0.1 -p 5432 -U postgres -d financeiq
```

В последней интерактивной сессии задайте разные пароли без передачи в аргументах команд или сохранения в SQL:

```text
\password financeiq_migrator
\password financeiq_runtime
\q
```

`createdb` выполняется один раз. Bootstrap повторяем: его запускает владелец БД, он не пересоздает БД, не сбрасывает пароли и отклоняет несовместимые атрибуты ролей, владение runtime объектами и опасные существующие grants. Роли создаются без паролей и не доступны по password-auth до их отдельного назначения.

| Роль | Разрешения |
| --- | --- |
| Администратор | Владелец БД, создание ролей и первоначальная настройка |
| `financeiq_migrator` | Владелец `public` и создаваемых объектов; миграции; без superuser/CREATEDB/CREATEROLE/replication/bypass RLS |
| `financeiq_runtime` | CONNECT и USAGE схемы; без владения, членства в других ролях, CREATE/TEMP, TRUNCATE и управления триггерами |

Runtime не имеет доступа к `alembic_version`. Права SELECT/INSERT/UPDATE/DELETE на предметные таблицы выдаются явно по необходимости в миграциях этапа 2, без `ALL` и blanket default grants. Новые функции migrator не получают стандартный `PUBLIC EXECUTE`; нужные разрешения задаются явно. Проверки владельца ресурсов и scoped repositories остаются обязательными.

Для приложения используйте `FINANCEIQ_DATABASE_URL` с `financeiq_runtime`. Для миграций — отдельный обязательный `FINANCEIQ_MIGRATION_DATABASE_URL` с `financeiq_migrator`; fallback на runtime URL отсутствует. `MigrationSettings` содержит только этот секрет; `load_migration_settings(*, env_file=None)` сохраняет те же правила валидации, приоритетов и безопасных ошибок, что и runtime-конфигурация.

Скопируйте `.env.migrations.example` в `.env.migrations` и замените синтетические credentials. Специальные символы в URL кодируются через percent-encoding. Не объединяйте этот файл с runtime `.env`: каждый загрузчик отклоняет чужие ключи. Файл сам не обнаруживается; без `-x env_file=...` Alembic читает только окружение процесса. Переменная процесса имеет приоритет над файлом.

Команды из `backend`:

```powershell
# PowerShell
.\.venv\Scripts\python.exe -m alembic -x env_file=../.env.migrations upgrade head
.\.venv\Scripts\python.exe -m alembic -x env_file=../.env.migrations current
.\.venv\Scripts\python.exe -m alembic -x env_file=../.env.migrations check
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic upgrade head --sql
```

```bash
# Linux
.venv/bin/python -m alembic -x env_file=../.env.migrations upgrade head
.venv/bin/python -m alembic -x env_file=../.env.migrations current
.venv/bin/python -m alembic -x env_file=../.env.migrations check
.venv/bin/python -m alembic heads
.venv/bin/python -m alembic upgrade head --sql
```

Текущая вершина — `0001`: техническая ревизия без предметных таблиц, создается только `alembic_version`. Offline `--sql` выводит DDL явно в stdout, без подключения или credentials. Online-ошибки конфигурации/подключения/DDL дают ненулевой код и фиксированное `Migration operation failed.` без SQL, параметров и исходной цепочки исключений. Режимы offline и online не настраивают root logging; собственные engine/pool логгеры отключены.

Миграции используют `db.base.Base.metadata`, отдельный async engine с `NullPool`, `run_sync` и транзакционный DDL. Будущие ORM-модели должны наследовать этот `Base` и явно регистрироваться импортами в migration environment. Имена CHECK задаются явно; общий naming convention формирует имена остальных ограничений и индексов. Autogenerate создает заготовку для ручной проверки, включая необходимые grants и триггеры.

## Проверки backend

Команды выполняются из `backend` после установки dev-окружения.

```powershell
# PowerShell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff format --check src tests migrations ../infra/compose
.\.venv\Scripts\python.exe -m ruff check src tests migrations ../infra/compose
.\.venv\Scripts\python.exe -m mypy
```

```bash
# Linux
.venv/bin/python -m pip check
.venv/bin/python -m pytest
.venv/bin/python -m ruff format --check src tests migrations ../infra/compose
.venv/bin/python -m ruff check src tests migrations ../infra/compose
.venv/bin/python -m mypy
```

`pytest` без путей запускает только 83 unit-теста без Docker и PostgreSQL. Существующие `unittest.TestCase` и `IsolatedAsyncioTestCase` сохранены; новые async-сценарии используют pytest-asyncio в strict mode с function-scoped event loop и фикстурами. Импорт тестов использует `importlib`, без изменения `PYTHONPATH`. Неизвестные маркеры и параметры конфигурации отклоняются. Кеш pytest исключен из Git.

Unit-тесты проверяют конфигурацию, HTTP, lifecycle, транзакции, отмены и безопасные ошибки с подменой DB-взаимодействия. Реальная PostgreSQL проверяется отдельным набором ниже. Starlette 1.10.0 выдает `StarletteDeprecationWarning` об HTTPX; предупреждение не подавляется.

Ruff проверяет правила `E4`, `E7`, `E9`, `F`, `I`, `UP`, `B`, включая импорты; длина строки 88, целевой Python 3.14, для `infra/compose/setup.py` — 3.11. Строгий mypy с плагином `pydantic.mypy` проверяет `src`, `migrations`, все тесты и скрипт подготовки Compose. Общих подавлений ошибок нет.

Для применения форматирования замените `ruff format --check` на `ruff format`; безопасные исправления lint выполняются через `ruff check --fix`, затем повторяются проверки. Для сборки wheel:

```powershell
.\.venv\Scripts\python.exe -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

```bash
.venv/bin/python -m pip wheel --no-deps --no-build-isolation --wheel-dir dist .
```

### Изолированная PostgreSQL: миграции и runtime

Нужен Docker Engine с Linux-контейнерами. Образ PostgreSQL 18.7 закреплен по digest в `tests/support/postgres.py`; Docker скачает его при необходимости. PostgreSQL client tools, локальные env-файлы и настроенные URL не требуются. При недоступном Docker или ошибке подключения выбранный набор завершается ошибкой, без skip.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration -m postgres
```

```bash
.venv/bin/python -m pytest tests/integration -m postgres
```

Набор включает 9 миграционных и 22 runtime-сценария. Общий helper создает независимые одноразовые контейнеры для двух наборов: случайные имена, пароли и loopback-порты, tmpfs, bootstrap и разделение ролей. Внешние DB URL не используются. Очистка выполняется также при ошибке подготовки; удаляются только собственные ресурсы, credentials не включаются в диагностику.

Миграционные тесты сохраняют приемку 1.6: upgrade/current/check, downgrade/upgrade, атомарный отказ DDL, bootstrap, ACL и безопасные ошибки. Runtime-набор проверяет настоящее сохранение и rollback, независимые соединения, отсутствие неявного commit, отложенный FK на commit, восстановление сессии, закрытого соединения в пуле и активного разорванного соединения, а также безопасный HTTP 500 и последующий успешный запрос.

`tests/support/probes.py` содержит отдельную ORM metadata и фабрики `build_parent` / `build_entry`. Фабрики создают объекты в памяти с UUID, Decimal и синтетическим текстом, без записи или commit. Перед каждым runtime-тестом migrator создает технические таблицы и явно выдает runtime только DML-права; после теста таблицы удаляются. Engines и сессии принадлежат event loop своего теста. Внешней откатываемой транзакции вокруг теста нет: commit и отложенные ограничения настоящие.

Денежные проверки подтверждают Decimal/NUMERIC roundtrip, знаки, точное `0.10 + 0.20 = 0.30`, границы `NUMERIC(18,2)`, переполнение и агрегат шире диапазона одной записи. Они не доказывают отклонение лишней дроби, NaN/∞ или нулевых финансовых фактов до записи: финансовая валидация и предметные guards реализуются на этапе 2. Технические таблицы не входят в metadata приложения, Alembic и рабочую схему.

### Изолированная проверка Compose

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration -m compose
```

```bash
.venv/bin/python -m pytest tests/integration -m compose
```

Тест сохраняет приемку 1.7: собирает образ, создает случайный Compose-проект, отдельные secrets, динамические порты и собственный volume. Проверяются миграции, HTTP/readiness, UID и разделение secrets, пересоздание контейнеров, сохранение данных, отказ/восстановление БД и неверный migration-пароль. В конце удаляются ресурсы только этого проекта, его образ и секреты; существующее локальное окружение не используется.

### Полный набор

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

```bash
.venv/bin/python -m pytest tests
```

Полный набор включает unit, PostgreSQL и Compose и требует Docker. Только сбор тестов без запуска: `python -m pytest tests --collect-only`. Для одного файла можно передать его путь вместо каталога. Автоматизация в GitHub Actions остается в задаче 1.9.

## Обновление зависимостей backend

Обычная установка использует готовый lock-файл. Для изменения зависимостей отредактируйте соответствующие точные версии в `pyproject.toml`, затем пересоздайте lock-файлы на каждой ОС с CPython 3.14.10. Сборочные зависимости, dev extra и хеши включаются настройками `[tool.pip-tools.compile]`.

Генератор запускается из dev-окружения с закрепленными pip и pip-tools. Если меняется сам генератор или setuptools, сначала установите новые закрепленные версии в отдельное окружение генерации и проверьте их совместимость. Lock-файлы вручную не редактируются.

```powershell
# PowerShell, из backend
.\.venv\Scripts\python.exe -m piptools compile --output-file requirements/dev-windows-py314.txt pyproject.toml
```

```bash
# Linux, из backend
.venv/bin/python -m piptools compile --output-file requirements/dev-linux-py314.txt pyproject.toml
```

Без `--upgrade` генератор сохраняет удовлетворяющие требованиям версии существующего lock-файла. Для намеренного обновления транзитивных зависимостей добавьте `--upgrade`; для одной зависимости — `--upgrade-package PACKAGE`. После изменения каждого lock-файла выполните установку в чистое окружение, все проверки и повторную генерацию без обновления: содержимое должно остаться прежним. Зафиксируйте исходную конфигурацию и оба lock-файла в одном изменении. Настройка CI выполняется в 1.9.

## Документация

- [Продуктовая спецификация](docs/PRODUCT.md) — назначение, возможности и бизнес-правила.
- [Доменная модель](docs/DOMAIN_MODEL.md) — сущности и финансовые инварианты.
- [Архитектура MVP](docs/ARCHITECTURE.md) — семь модулей, monorepo, транзакции, взаимодействие и ресурсы; пакет 0.14 согласован 2026-11-02.
- [Схема PostgreSQL](docs/DATABASE_SCHEMA.md) — структура данных и ограничения целостности.
- [Реестр решений](docs/DECISIONS.md) — согласованные подходы, принятые технические пакеты 0.13/0.14 и открытые вопросы.
- [Матрица финансовых инвариантов](docs/FINANCIAL_INVARIANTS.md) — проверки сервиса/БД и сценарии будущих тестов, включая конкуренцию и отмены.
- [Разбор Money Manager](docs/IMPORT_FORMAT_REVIEW.md) — наблюдения исходного образца и основания выбранных правил.
- [Контракт импорта](docs/IMPORT_FORMAT.md) — согласованный профиль v1 и синтетический XLSX с ожидаемыми результатами.
- [Идентичность и повторы](docs/IMPORT_IDENTITY.md) — согласованные правила повторного импорта и 21 сценарий будущей приемки.
- [Ошибки импорта](docs/IMPORT_ERRORS.md) — согласованные правила блокировки Confirm, допустимые действия и 22 сценария будущей приемки; пункт 0.9 завершен как задача проектирования.
- [Лимиты и хранение](docs/IMPORT_LIMITS.md) — согласованные пределы ресурсов, срок и восстановление preview, очистка и резервные копии, 17 сценариев будущей приемки; пункт 0.10 завершен как задача проектирования.
- [Исправления и отмена импорта](docs/FACT_LIFECYCLE.md) — согласованные границы ручных изменений, отмена всей книги, повторный импорт, архив счетов и 24 сценария будущей приемки; пункт 0.11 завершен как задача проектирования.
- [Категории и правила](docs/CATEGORIES.md) — согласованные уточнения D-10 и 25 сценариев будущей приемки; пункт 0.12 завершен как задача проектирования.
