# FinanceIQ — PostgreSQL-схема MVP

**Статус:** PostgreSQL-схема финансового фундамента согласована пользователем 2026-11-02 в пакете 0.13. Продуктовые правила 0.1–0.12 сохранены. По согласованному плану 2.1 от 2026-11-19 реализованы users и auth_sessions, уточнен блок хранения Auth D-12 (§2). В 2.2 реализованы Accounts, подтверждения остатков/полноты и основа аудита (§3, §7.1). В 2.3 реализовано хранение Categories и чистая нормализация текста (§4). В 2.4 добавлено хранение Imports и базовые guards жизненного цикла (§5). Остальные предметные таблицы и полные guards остаются на следующих задачах этапа 2; открытые детали D-11 и HTTP-параметры Auth не утверждаются.

**Дата:** 2026-11-20.

**Основания:** [PRODUCT.md](PRODUCT.md), [DOMAIN_MODEL.md](DOMAIN_MODEL.md), [DECISIONS.md](DECISIONS.md), контракты [импорта](IMPORT_FORMAT.md), [повторов](IMPORT_IDENTITY.md), [ошибок](IMPORT_ERRORS.md), [хранения](IMPORT_LIMITS.md), [жизненного цикла](FACT_LIFECYCLE.md) и [категорий](CATEGORIES.md).

Матрица финансовых инвариантов, сервисных проверок, ограничений БД и будущих тестов находится в [FINANCIAL_INVARIANTS.md](FINANCIAL_INVARIANTS.md). Здесь задано физическое представление для этой матрицы. Архитектура модулей и механизмы ограничения ресурсов описаны в [архитектуре 0.14](ARCHITECTURE.md), согласованной 2026-11-02; приложение не создается.

## 1. Соглашения и границы

Инфраструктура 1.6 (2026-11-16) использует PostgreSQL 18.7 и защищенную схему `public`. Владелец БД — отдельный администратор; `financeiq_migrator` владеет схемой и создаваемыми объектами, `financeiq_runtime` получает только CONNECT/USAGE и явные разрешения на данные. Runtime не владеет объектами, не входит в другие роли, не имеет superuser/CREATEDB/CREATEROLE/replication/bypass RLS, CREATE/TEMP, TRUNCATE и возможности отключать триггеры. `alembic_version` доступна только миграциям/администратору. Скрипт [bootstrap](../infra/postgres/bootstrap.sql) не создает предметные таблицы и не меняет пароли существующих ролей.

На этапе 2 каждая миграция явно выдает runtime только необходимые SELECT/INSERT/UPDATE/DELETE и разрешения на функции; blanket default grants для таблиц не используются. Default `PUBLIC EXECUTE` новых функций migrator отозван на уровне БД. Единственный `db.base.Base.metadata` использует общий naming convention; CHECK требуют явного имени. Техническая ревизия `0001` фиксирует историю Alembic без реализации приведенных ниже таблиц. Реальные проверки 1.6 подтверждают ACL и атомарность DDL на синтетических объектах; предметные guards C-01–C-11 остаются на этапе 2.

Целевая схема включает 17 таблиц в общей PostgreSQL-БД без расширений: финансовый фундамент согласован в 0.13, блок хранения Auth отдельно уточнен в 2.1. Результат принятия книги представлен неизменяемой принятой попыткой и результатами ее движений; дублирующая таблица результата не нужна.

- В каждой таблице `id UUID PRIMARY KEY`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. UUID создает приложение. Кроме `users`, везде `user_id UUID NOT NULL REFERENCES users(id)`.
- `NN` означает NOT NULL; у nullable-полей default NULL. Другие defaults указаны явно. У целей составных FK есть соответствующий обычный `UNIQUE(user_id, id)` и, где нужно, более широкий UNIQUE. Эти целевые ключи не являются отложенными.
- FK включают владельца и необходимую область попытки/счета; `ON DELETE RESTRICT ON UPDATE RESTRICT`. Optional FK используют MATCH SIMPLE. Дополнительный FK без nullable-компонента сохраняет проверку владельца, если часть широкой ссылки еще не заполнена.
- Идентификаторы, владелец и время создания неизменяемы. Финансовая история не удаляется. Исключение — ограниченная очистка **непринятого** содержимого по §5.6; удаления учетной записи в этой схеме нет.
- Статусы — TEXT с CHECK, без PostgreSQL ENUM. Дни — DATE, технические события — TIMESTAMPTZ; финансовые даты и границы интервалов конечны (CHECK isfinite, для nullable staging с IS NULL). Отчеты используют `[start_date, end_date)`, полнота — включительный интервал.
- Суммы фактов и опор — NUMERIC(18,2), конечны: `value BETWEEN -9999999999999999.99 AND 9999999999999999.99`. Факты дополнительно `amount <> 0`; у опоры ноль и отрицательное значение допустимы. Nullable staging проверяется через `value IS NULL OR ...`.
- Исходное число проверяется через Decimal **до SQL**: NUMERIC(18,2) округляет лишнюю дробь при приведении, CHECK уже не видит исходный ввод. Агрегаты SUM не приводятся к размеру отдельной строки. [Основание PostgreSQL](https://www.postgresql.org/docs/current/datatype-numeric.html).
- Точные сравнения исходных названий, `name_key` и кодов не зависят от нечувствительной к регистру/диакритике collation; для текстовых ключей используется `COLLATE "C"`.
- SQL-ограничение не доказывает принадлежность реального банковского счета, сверку с выпиской или нажатие кнопки человеком. Сервер проверяет сессию и решение, БД — атомарное сохранение доказательства и согласованность ссылок. БД не повторяет XLSX-парсер и Python Unicode-нормализацию.

Нет обязательных финансовых столбцов, зависящих от открытого D-11. Поля Auth в §2 согласованы для 2.1; HTTP/CSRF, TTL, продление и очистка сессий определяются перед этапом 3.

## 2. Auth: хранение D-12, согласовано в 2.1

Реализовано миграцией `0002` поверх неизмененной технической `0001`. Модели находятся в Auth; явный реестр `db.models.metadata` подключает их к единой `Base.metadata` в Alembic. У обеих таблиц общие UUID/created_at из §1; UUID назначает приложение, created_at конечен.

### 2.1. `users`

| Поле сверх общих | Тип / ограничение |
| --- | --- |
| email | TEXT COLLATE "C" NN UNIQUE; непустые печатные ASCII без whitespace, только нижний регистр |
| password_hash | TEXT NN, не пустая строка; алгоритм определяется на этапе 3 |
| timezone | TEXT NN, не пустая строка, без default |
| is_active | BOOLEAN NN DEFAULT true |

`normalize_email` сначала удаляет внешние whitespace через Python str.strip(), затем требует только U+0021–U+007E и приводит ASCII к нижнему регистру. Точки и `+suffix` сохраняются, преобразований провайдеров/IDNA нет. CHECK требует `email = lower(email) AND email ~ '^[!-~]+$'`; отдельного email_key и расширения citext нет. Полный синтаксис email и существование IANA-зоны проверяет будущий сервис Auth. Строка users остается корнем сериализации финансовых изменений владельца (§8); реализация финансового протокола — последующие задачи.

### 2.2. `auth_sessions`

| Поле сверх общих | Тип / ограничение |
| --- | --- |
| user_id | UUID NN FK users(id), ON DELETE RESTRICT ON UPDATE RESTRICT; B-tree индекс |
| token_hash | BYTEA NN UNIQUE, octet_length = 32 |
| csrf_token_hash | BYTEA NN, octet_length = 32; без UNIQUE |
| expires_at | TIMESTAMPTZ NN, конечен и строго позже created_at |
| revoked_at | TIMESTAMPTZ NULL, при заполнении конечен и не раньше created_at |

Контракт будущей выдачи: два независимых случайных секрета по 32 байта, хранение только SHA-256 digest. Сырых токенов в БД нет. Истекшая сессия допустима в таблице, отзыв после истечения разрешен; текущую действительность проверяет будущий сервис. TTL не выводится из TTL импорта. Генерация секретов и HTTP/CSRF-поведение в 2.1 не реализованы.

### 2.3. Полномочия и откат Auth

`financeiq_runtime` получает SELECT/INSERT обеих таблиц и только UPDATE(email, password_hash, timezone, is_active) для users, UPDATE(expires_at, revoked_at, csrf_token_hash) для auth_sessions. Нет UPDATE идентификаторов/владельца/created_at/token_hash, DELETE, TRUNCATE или DDL. Политика очистки сессий остается на этапе 3. Отдельных составных ключей без потребителя не добавлено.

Downgrade `0002 → 0001` берет ACCESS EXCLUSIVE обеих таблиц и до DROP проверяет их пустоту. При наличии записей откат прекращается атомарно, данные и номер ревизии сохраняются; пустая схема удаляется в порядке auth_sessions → users. Проверки выполняются только на одноразовой тестовой БД.

## 3. Accounts

Хранение реализовано ревизией `0003` в задаче 2.2 от 2026-11-19: три модели Accounts и основа `financial_actions` (§7.1) зарегистрированы в общей metadata. Локальные CHECK/FK/UNIQUE и индексы ниже созданы; полные семантические guards действий, C-10 и финансовый протокол блокировок еще не реализованы.

### 3.1. `accounts`

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `name` | TEXT | NN | Отображаемое название |
| `currency_code` | TEXT | NN | Неизменяемое RUB |
| `archived_at` | TIMESTAMPTZ | NULL | Обратимый архив |
| `facts_version` | BIGINT | NN, 0 | Монотонная версия состава действующих фактов |

CHECK непустого имени до 100 кодовых точек, `currency_code = 'RUB'`, `facts_version >= 0`, `archived_at IS NULL OR archived_at >= created_at`. UNIQUE `(user_id, id)`. Имена счетов не уникальны. Триггер увеличивает facts_version при INSERT факта и его исключении, но не при link/классификации. Клиент не задает версию.

Переименование, архив и восстановление не меняют происхождение или участие счета в общих расчетах. Новый Confirm требует активных целевых счетов; чтение/повтор прежнего результата, отмена, классификация и опоры/полнота доступны также для архива. Архив/восстановление сопровождаются financial_actions. Индекс `(user_id, archived_at, id)`.

### 3.2. `balance_confirmations`

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | UUID | NN | Собственный счет |
| `balance_date` | DATE | NN | Завершенный день, EOD |
| `balance` | NUMERIC(18,2) | NN | Сумма по выписке, включая ноль/отрицательную |
| `confirmation_action_id` | UUID | NN | Явное пользовательское подтверждение |
| `revocation_action_id` | UUID | NULL | Однократный отзыв с причиной |

Составные FK на accounts и financial_actions. Частичный UNIQUE `(user_id, account_id, balance_date) WHERE revocation_action_id IS NULL`. Исходные значения и подтверждение неизменяемы; разрешен только отзыв NULL → ID действия. Время подтверждения/отзыва берется из неизменяемого действия.

Сервис проверяет завершенность дня в пользовательской зоне и точность ввода. Исправление — отзыв и новая опора атомарно. Автоматического отзыва при изменении фактов нет. Расхождение показывается без балансирующей операции. Индекс истории `(user_id, account_id, balance_date DESC, id)`.

### 3.3. `history_confirmations`

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | UUID | NN | Собственный счет |
| `start_date`, `end_date` | DATE | NN | Включительный диапазон |
| `observed_facts_version` | BIGINT | NN | Версия состава на момент сверки |
| `observed_facts_digest` | BYTEA | NN | Digest показанного набора фактов интервала, включая пустой |
| `confirmation_action_id` | UUID | NN | Явное подтверждение полноты |
| `revocation_action_id` | UUID | NULL | Явный отзыв либо импорт/отмена, изменившие факты |

FK владельца счета и обоих действий; CHECK `start_date <= end_date`, версии >= 0 и длины digest 32. Частичный UNIQUE `(user_id, account_id, start_date, end_date) WHERE revocation_action_id IS NULL`. Пересечения разрешены, объединяются сервисом. Индекс действующих интервалов `(user_id, account_id, end_date, start_date) WHERE revocation_action_id IS NULL`.

Подтверждение проверяет просмотренный набор после блокировки (§7). Отсутствие операций без такого действия не дает полноту. Изменение набора внутри диапазона требует повторной сверки; версия счета помогает обнаружить изменение, но изменение вне диапазона при том же наборе не обязано отвергать план. Добавление/исключение факта в том же commit целиком отзывает **все** действующие интервалы, содержащие его дату. Причина отзыва ссылается на действие и затронутые факты; опоры сохраняются. Новое подтверждение — новая запись, автоматического восстановления нет.

### 3.4. Реализованные полномочия и границы 2.2

Runtime получает SELECT всех четырех новых таблиц и column-level INSERT. Для accounts разрешены только id/user_id/name/currency_code; archived_at и facts_version назначаются defaults NULL/0. Для подтверждений INSERT не включает created_at и revocation_action_id; для действий — created_at. Техническое время назначает БД; у действий используется clock_timestamp(), у остальных — now(). UPDATE доступен только для name/archived_at счета и revocation_action_id подтверждений. UPDATE financial_actions, DELETE, TRUNCATE и DDL runtime недоступны.

Немедленный BEFORE UPDATE guard обеих таблиц подтверждений сравнивает все исходные поля и допускает только первый отзыв NULL → action_id. Повтор без изменения состояния разрешен, смена/снятие отзыва и изменение исходных полей запрещены также при обходе column-level ACL. FK проверяют владельца действия; его тип, целевой счет, причина и содержимое доказательства проверяются позднее в 2.7. Переименование/архив пока защищены только локальными CHECK и правами, обязательность сопровождающего действия остается в C-11. Runtime не задает и не меняет facts_version; ее увеличение вместе с отзывом полноты появится с C-10 после Transactions.

Downgrade `0003 → 0002` берет ACCESS EXCLUSIVE четырех таблиц и до DROP отказывает при наличии любых новых записей. Откат пустых новых таблиц сохраняет существующий Auth; отказ сохраняет данные и ревизию атомарно. Проверка завершенности дня в пользовательской зоне, точности денежного ввода до SQL, digest просмотренного набора, объединение интервалов и расчет остатка остаются на этапе 4. NUMERIC(18,2) не заменяет проверку лишней дроби до приведения.

## 4. Categories

Хранение реализовано в задаче 2.3 от 2026-11-19: Category/CategorizationRule, общая metadata и ревизия `0004` поверх `0003`. В модуле Categories добавлены чистые `category_text_v1`, `normalize_category_name` (имя и ключ вместе), `normalize_rule_pattern`; используются CPython 3.14.10 и Unicode 16.0.0. Валидаторы отображаемых значений проверяют исходную и нормализованную длину и пустоту; для имени также проверяется зарезервированное значение. category_text_v1 допускает пустое описание; расширившиеся ключи не обрезаются. HTTP, repositories и сервис назначения не добавлены.

### 4.1. `categories`

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `name` | TEXT | NN | Отображаемое имя после NFC/сворачивания пробелов |
| `name_key` | TEXT COLLATE "C" | NN | Производный ключ category_text_v1 |
| `kind` | TEXT | NN | Неизменяемое income/expense |
| `archived_at` | TIMESTAMPTZ | NULL | Архив без удаления назначений |

CHECK `kind IN ('income','expense')`, длины имени 1–100, непустого name_key и запрета ключа `без категории`; корректности archived_at. UNIQUE `(user_id, id)`, частичный UNIQUE `(user_id, kind, name_key) WHERE archived_at IS NULL`. У ключа нет лимита 100: casefold может расширить текст.

Сервис одной функцией category_text_v1 вычисляет ключ при create/rename и передает имя/ключ вместе. Немедленный UPDATE-guard запрещает смену kind; DELETE для runtime запрещен отсутствием прав. UNIQUE проверяет **переданный** ключ; тождество ключа Python-нормализации не обещается произвольному SQL. Runtime-запись доступна только серверу, версии Python/Unicode фиксируются в этапе 1; SQL lower(btrim(...)) не заменяет алгоритм. Эта граница тестируется через серверные пути (CAT-05–CAT-11).

Архив в одной транзакции выключает активные правила, восстановление проверяет свободное имя и не включает их обратно. История использует текущее название. При регистрации создаются десять персональных категорий default_categories_v1 без правил атомарно с пользователем; вход/импорт этого не повторяют.

### 4.2. `categorization_rules`

Поля: `category_id UUID NN`, `pattern TEXT NN`, `priority INTEGER NN`, `is_active BOOLEAN NN DEFAULT true`. FK `(user_id, category_id) → categories(user_id, id)`; UNIQUE `(user_id, id)`; CHECK `priority > 0`, длины отображаемого pattern 1–200. NFC/пробелы и непустоту после нормализации проверяет сервис. Ключ сопоставления вычисляется category_text_v1, его версия не смешивается с fingerprint.

Итоговая уникальность активных приоритетов обеспечивается **отложенным** UNIQUE `(user_id, active_priority) DEFERRABLE INITIALLY DEFERRED`, где `active_priority INTEGER GENERATED ALWAYS AS (CASE WHEN is_active THEN priority ELSE NULL END) STORED`. Обычная семантика NULL допускает совпадения неактивных. Так обмен 10 ↔ 20 не зависит от промежуточного состояния; частичный UNIQUE здесь не используется, поскольку он не откладывается. Сервис сериализует перестановки и валидирует итог до записи. [Свойства UNIQUE](https://www.postgresql.org/docs/current/ddl-constraints.html).

Активное правило требует активной категории (C-05). Полный порядок читается одним снимком. Правило меняет только последующие назначения; старую операцию нельзя сверять с нынешней целью отредактированного правила. DELETE запрещен. Индекс `(user_id, category_id)`.

### 4.3. Реализованные полномочия и границы 2.3

Runtime имеет SELECT обеих таблиц и column-level INSERT: для categories — id/user_id/name/name_key/kind; для categorization_rules — id/user_id/category_id/pattern/priority/is_active. UPDATE ограничен name/name_key/archived_at категории и category_id/pattern/priority/is_active правила. id/user_id/created_at неизменяемы для runtime; created_at и начальный архив назначаются БД, active_priority вычисляется БД. DELETE/TRUNCATE/DDL и прямой EXECUTE guard-функции не выдаются. CHECK/FK/UNIQUE действуют также при прямом SQL; соответствие name_key Python-нормализации обеспечивает серверный путь, SQL lower не используется.

Downgrade `0004 → 0003` блокирует categories/categorization_rules в ACCESS EXCLUSIVE и отказывает до DROP при любых строках, включая архивные категории и выключенные правила. Пустые новые таблицы и guard-функция удаляются, прежние Auth/Accounts сохраняются. Начальный набор миграцией не создается: он остается частью атомарной регистрации этапа 3. C-05 и связанные межстрочные guards остаются в 2.7, общий протокол блокировок — в 2.8; хранение само по себе пока не запрещает активное правило на архивную категорию. API, применение правил и управление жизненным циклом — этап 5.

## 5. Imports

**Реализованная часть 2.4 (2026-11-20):** шесть моделей Imports и ревизия `0005` поверх `0004`, доступные CHECK/FK/UNIQUE и индексы этого раздела. Принятый результат представлен `ImportAttempt`, без отдельной таблицы результата. `ImportMovement.result_transaction_id`, FK на Transactions, уникальность результата внутри попытки и CHECK зависимости decision/result добавляются в 2.5; в текущей схеме этих элементов нет. Ниже описана полная целевая схема, включая отложенные элементы.

### 5.1. `import_batches`: постоянная идентичность книги

Поля: `source_code TEXT NN`, `format_code TEXT NN`, `file_sha256 BYTEA NN`, `byte_size BIGINT NN`. CHECK `source_code = 'money_manager'`, `format_code = 'money_manager_xlsx_v1'`, hash длиной 32 и размер 1–10 485 760. UNIQUE `(user_id, id)` и `(user_id, source_code, file_sha256)`. Индекс истории `(user_id, created_at DESC, id)`.

У batch **нет** account_id, банковского ID, preview TTL или терминального confirmed. Имя файла не хранится в минимальной записи. Смена парсера/сопоставлений не меняет идентичность. Состояние книги выводится из попыток и отмен; после очистки остаются batch и безопасные метаданные попыток.

### 5.2. `import_attempts`: временная работа и неизменяемый принятый результат

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `batch_id` | UUID | NN | Идентичность книги |
| `attempt_number` | INTEGER | NN | Положительный номер внутри batch |
| `status` | TEXT | NN, processing | processing / preview / rejected / expired / abandoned / accepted |
| `received_at`, `expires_at` | TIMESTAMPTZ | NN | Полный прием и received_at + 86 400 секунд |
| `parser_version` | TEXT | NN | Поддерживаемая версия реализации разбора |
| `excel_date_system` | TEXT | NULL | 1900 / 1904; обязательна после полного корректного разбора |
| `preview_version` | INTEGER | NN, 1 | Версия плана только этой попытки |
| `preview_digest` | BYTEA | NULL | Серверный digest плана; после принятия неизменяем |
| `preview_plan` | JSONB | NULL | Временный серверный план по §7.2 |
| `source_row_count`, `movement_count` | INTEGER | NULL | Достоверные счетчики полного разбора |
| `content_present` | BOOLEAN | NN, true | Есть временное/принятое содержимое |
| `content_purged_at` | TIMESTAMPTZ | NULL | Очистка непринятого содержимого |
| `error_code` | TEXT | NULL | Безопасная причина без исходных значений |
| `accepted_action_id` | UUID | NULL | Успешный Confirm и его серверное время |

FK batch и действия включают user_id; UNIQUE `(user_id, id)`, `(user_id, batch_id, attempt_number)`. Частичный UNIQUE `(user_id, batch_id) WHERE status IN ('processing','preview','rejected')` допускает одну текущую неподтвержденную попытку. Новая после rejected требует явного прекращения прежней; после expired/abandoned — нового полного приема. Действующее принятие запрещает новую попытку (C-08). После отмены требуется отдельная явная команда D-13.

CHECK перечислений, положительных версий, длины digest, JSON-объекта плана, `expires_at = received_at + interval '86400 seconds'`; `status = 'accepted'` эквивалентно наличию accepted_action_id. Счетчики при наличии: 0–10 000 строк и 0–20 000 движений; их соответствие дочерним записям проверяет C-06. Accepted требует положительных счетчиков, digest, полного разбора и content_present. `content_present = false` требует content_purged_at и запрещает accepted; очистка обнуляет preview_digest/plan, excel_date_system и удаляет временные решения/значения. Безопасные версии/счетчики/времена/код причины могут остаться.

```text
processing → preview | rejected | expired | abandoned
preview → preview (новая версия/digest) | accepted | expired | abandoned
rejected → expired | abandoned
accepted → неизменяемый результат; отмена отдельной записью
expired / abandoned → терминальные; новый разбор дает другую попытку
```

Истечение определяется по времени, даже если status еще не обновлен очисткой. Полностью разобранная книга с ошибками может иметь status preview с диагностикой: статус сам по себе не разрешает Confirm. rejected требует безопасного error_code. Индексы `(user_id, batch_id, attempt_number DESC)` и `(expires_at, id) WHERE content_present AND status <> 'accepted'`; явное прекращение добавляет попытку в ближайшую очистку независимо от expires_at.

### 5.3. `import_account_mappings`

Поля: `attempt_id UUID NN`, `source_account_name TEXT COLLATE "C" NN`, `account_id UUID NULL`. Исходный текст сохраняется точно; пустой ввод отклоняет парсер. CHECK длины 1–8 192. UNIQUE `(user_id, attempt_id, id)`, `(user_id, attempt_id, id, account_id)`; FK попытки и собственного счета.

**Техническая корректировка, согласована 2026-11-20 при реализации 2.4:** точная уникальность имени внутри `(user_id, attempt_id)` обеспечивается SQL-guard под блокировкой попытки и неуникальным индексом `(user_id, attempt_id, md5(source_account_name))` для поиска. Широкий UNIQUE по полному имени не используется: PostgreSQL отклоняет допустимые несжимаемые строки длиной 8 192 из-за ограничения размера B-tree ключа. После получения блокировки VOLATILE guard сравнивает полный текст с COLLATE "C"; совпадение MD5 само по себе не означает дубликат. MD5 здесь только поисковый bucket, не идентичность или криптографическое доказательство. Лимит и бизнес-правило точного сравнения сохранены.

NULL account_id означает незавершенное сопоставление и блокирует Confirm. Все используемые исходные названия сопоставляются явно; разные имена не объединяются автоматически. Принятые сопоставления неизменяемы. Автоматического создания счета нет.

Запись сопоставлений допускается только при согласованном READ COMMITTED; guard отклоняет другой уровень изоляции. Это сохраняет точную уникальность после ожидания блокировки: фиксированный snapshot REPEATABLE READ не обязан увидеть ранее завершившуюся вставку при неизменной родительской строке. Read-only аналитика в REPEATABLE READ этим не ограничивается.

### 5.4. `import_rows`: одна исходная запись

Поля: `attempt_id UUID NN`, `sheet_name TEXT NN`, `row_number INTEGER NN`, `source_values JSONB NULL`, `raw_payload JSONB NULL`, `validation_errors JSONB NN DEFAULT '[]'`.

FK попытки; UNIQUE `(user_id, attempt_id, id)` и `(user_id, attempt_id, sheet_name, row_number)`. CHECK sheet_name из трех точных имен, `row_number BETWEEN 3 AND 1048576`, формы JSON (объекты/массив). Физический номер Excel не ограничивается 10 000. Ошибочные/скрытые непустые строки учитываются в source_row_count. Ошибки структуры всей книги и незавершенные действия дополнительно входят в план попытки.

`source_values` — версионированный нормализованный объект money_manager_xlsx_v1. Он содержит **все** согласованные реквизиты: точное числовое значение локальной даты/времени Excel строкой Decimal вместе с системой дат попытки; исходные названия счетов, категорию, теги, комментарий; величины и обе валютные пары (включая отсутствие необязательной пары), у перевода — обе стороны. Денежные числа в JSON — строки. Типы/ключи и лимиты 8 192/2 000 проверяет сервер; наличие принятого содержимого — C-06. Значения не trim/casefold/обрезаются для прохождения лимита.

raw_payload нужен только для временной диагностики и очищается при Confirm; он не заменяет source_values. Принятое нормализованное происхождение неизменяемо. Ошибочная строка не превращается в факт и не пропускается.

### 5.5. `import_movements`: стороны, решения и постоянные наблюдения

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `attempt_id`, `row_id`, `mapping_id` | UUID | NN | Попытка, исходная запись и сопоставление |
| `side` | TEXT | NN | single / outgoing / incoming |
| `account_id` | UUID | NULL | Из mapping для FK/поиска, не свободный выбор строки |
| `occurred_on` | DATE | NULL | Проверенный локальный день |
| `amount` | NUMERIC(18,2) | NULL | Проверенная подписанная сумма |
| `source_comment` | TEXT | NULL | Точный комментарий, пустая ячейка дает пустую строку |
| `fingerprint_version` | TEXT | NN, money_manager_match_v1 | Версия fingerprint |
| `fingerprint` | BYTEA | NULL | Неуникальный SHA-256 наблюдения |
| `decision` | TEXT | NN, pending | pending / insert / link |
| `result_transaction_id` | UUID | NULL | Существующий факт link, новый факт принятого insert |

FK `(user_id, attempt_id, row_id) → import_rows(user_id, attempt_id, id)`; аналогичный FK на mapping без account_id и дополнительный с account_id; FK `(user_id, account_id, result_transaction_id) → transactions(user_id, account_id, id)`. Последний DEFERRABLE INITIALLY DEFERRED для циклического создания происхождения и факта. UNIQUE `(user_id, account_id, id)`, `(user_id, attempt_id, row_id, side)`, `(user_id, attempt_id, result_transaction_id)` с обычной семантикой NULL. Последний запрещает два результата одного факта внутри попытки, включая insert, а не только два link.

CHECK перечислений, `fingerprint_version = 'money_manager_match_v1'`, конечной ненулевой суммы при наличии, комментария <= 2 000, fingerprint 32 байта. pending требует NULL результата; link — непустую ссылку; insert получает ее при принятии. C-06 проверяет обязательность нормализованных полей, результатов и число сторон: одна single у обычной строки либо outgoing/incoming у полной строки перевода. Неполная строка источника не создает фиктивное движение. Реальная сторона из обычной записи может отдельно подтверждаться как transfer по D-04.

Предлагаемые типы/категории, кандидаты, различия и согласия находятся во временном плане (§7). После принятия необходимые решения сохраняются в financial_actions/action_transactions; insert/link и результат — здесь. Link не переписывает классификацию. Принятое наблюдение сохраняется независимо от отмены.

Fingerprint не UNIQUE. Индекс `(user_id, account_id, fingerprint_version, fingerprint)` ищет наблюдения; основной поиск — по действующим transactions, затем DISTINCT по ID. Банковских полей и UNIQUE по дате/сумме/описанию нет.

### 5.6. Очистка и `import_cancellations`

`import_cancellations`: `attempt_id UUID NN`, `action_id UUID NN`. UNIQUE `(user_id, attempt_id)` и `(user_id, id)`; FK попытки и действия владельца. Попытка должна быть accepted. Причина, версия/digest плана, инициатор, время и исключенные ID — в действии/элементах. Отмена неизменяема; ноль исключенных insert допустим. Наблюдения сохраняются, но перестают быть действующими по наличию отмены.

Только непринятые строки/движения/сопоставления/планы/диагностика удаляются после истечения либо прекращения работы. Очистка атомарно удаляет дочернее содержимое и выставляет content_present=false; guards разрешают это лишь для терминальной непринятой попытки без финансовых ссылок. Сохраняются batch и безопасные метаданные, другие попытки не затрагиваются. Принятый результат, включая отмененный, не подпадает под TTL.

Три слота D-09 считаются по **различным batch с непринятым сохраненным содержимым**, включая ошибки и ожидающее очистки содержимое; новая попытка той же книги не создает четвертый слот. Подсчет под блокировкой владельца. Часовая очистка/запуск после аварии, ограничения парсера и резервные копии — по IMPORT_LIMITS.md; механизмы согласованы в [ARCHITECTURE.md](ARCHITECTURE.md), §§6–7, в пакете 0.14 2026-11-02. Исходные XLSX не хранятся постоянно.

### 5.7. Реализованные полномочия и guards 2.4

Runtime получает SELECT и column-level INSERT/UPDATE: id, user_id, created_at, исходная принадлежность и координаты не редактируются. Batch и отмены доступны только для чтения/вставки; DELETE попыток, batch, действий и отмен не выдается. Новая попытка начинается с processing и content_present=true. Guard сохраняет ее исходные параметры, проверяет переходы статусов и запрещает изменения принятой/очищенной попытки; терминальные состояния разрешают только очистку. Изменение плана/digest уже открытого preview требует увеличения версии и нового digest. Повторная проверка полного плана остается сервисной обязанностью.

Дочерние INSERT/UPDATE разрешены только при processing/preview; guard блокирует родительскую попытку через FOR NO KEY UPDATE и заново проверяет ее состояние. DELETE дочернего содержимого разрешен только при expired/abandoned. Отложенные constraint triggers требуют в одной транзакции удалить все движения/строки/сопоставления и выставить content_present=false с временем очистки, обнулением preview_plan/digest/excel_date_system. Частичная очистка откатывается; принятые, включая отмененные, данные не очищаются. Raw и временный план принятия убираются до перехода в accepted, в той же транзакции. Отмена создается только для accepted и неизменяема.

FinancialAction расширена source_attempt_id с FK владельца и типами import_confirm/import_cancel. Обратный FK создается после таблиц; ORM использует use_alter. Downgrade блокирует все шесть таблиц и аудит и отказывает при любых Imports-данных, заполненном source_attempt_id или импортном action_type. Пустой откат сохраняет Auth/Accounts/Categories и прежний аудит.

Это базовая защита хранения, без реализации Upload/Parse/Preview/Confirm, отмены и lifecycle-процесса очистки. Полные C-06–C-11, смысл/цель доказательства действия, единое действующее принятие и запрет новой попытки при нем остаются в 2.7; общий корень владельца/порядок блокировок — в 2.8. Локальные блокировки попытки не заменяют этот протокол. Лимиты слотов/ресурсов, исходная точность до SQL, структура source_values, расчет digest и проверки срока после ожидания реализуются вместе с процессом импорта.

## 6. Transactions

### 6.1. `transactions`

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | UUID | NN | Собственный счет |
| `source_movement_id` | UUID | NN | Единственное породившее наблюдение insert |
| `occurred_on` | DATE | NN | Неизменяемый пользовательский день |
| `amount` | NUMERIC(18,2) | NN | Неизменяемая подписанная сумма |
| `description` | TEXT | NN | Неизменяемый комментарий исходного движения |
| `kind` | TEXT | NN, unclassified | income / expense / transfer / refund / unclassified |
| `category_id` | UUID | NULL | Прямая категория |
| `category_origin` | TEXT | NN, none | none / rule / manual |
| `categorization_rule_id` | UUID | NULL | Происхождение автоматического назначения |
| `refund_of_transaction_id` | UUID | NULL | Оригинал linked refund |
| `refund_action_id` | UUID | NULL | Доказательство текущей refund-связи либо подтверждения несвязанного refund |
| `last_action_id` | UUID | NN | Последнее действие классификации/связей/категории |
| `revision` | BIGINT | NN, 1 | Серверная версия для планов и аудита |
| `cancelled_by_id` | UUID | NULL | Необратимое исключение по отмене принятия |

Составные FK на счет, категорию, правило, оригинал возврата, оба действия, отмену. FK `(user_id, account_id, source_movement_id) → import_movements(user_id, account_id, id)`. UNIQUE `(user_id, id)`, `(user_id, account_id, id)`, `(user_id, source_movement_id)`. C-07 проверяет взаимность insert ↔ source_movement ↔ result_transaction и равенство счета/даты/суммы/комментария. Link хранится в других movements, не меняет source_movement_id.

Локальные CHECK: перечисления; конечная ненулевая сумма; income > 0, expense < 0; description <= 2 000; revision > 0. Refund-ссылка допустима только у refund и не на себя. Категория:

```text
none   → category_id IS NULL AND categorization_rule_id IS NULL
rule   → category_id IS NOT NULL AND categorization_rule_id IS NOT NULL
manual → categorization_rule_id IS NULL   (category_id может быть NULL)
kind IN ('transfer','unclassified') → category_origin = 'none'
refund_of_transaction_id IS NOT NULL → category_origin = 'none'
kind = 'refund' → category_origin <> 'rule'
kind = 'refund' AND refund_of_transaction_id IS NULL → category_origin = 'manual'
```

Импликации переводятся в SQL с IS NULL, без обхода через неизвестное значение CHECK. Дополнительно `(kind = 'refund') = (refund_action_id IS NOT NULL)`. refund_action_id меняется только при изменении смысла/связи refund, не при назначении категории: это сохраняет происхождение действующей связи для проверки отмены D-13, §4. Эффективная категория linked refund вычисляется из оригинала, включая архивную. Несвязанный refund требует отдельного подтверждения смысла и ручного решения, включая допустимое отсутствие категории; одно отсутствие FK не доказывает согласие.

Действующий факт: `cancelled_by_id IS NULL`. Новые link/transfer/refund допускают только действующие факты. Отмена не меняет исходные значения и не обращается обратно в NULL. Классификация отмененного факта запрещена. Guard защищает источник; INSERT возможен только вместе с принятием insert той же попытки (C-06/C-07), DELETE запрещен.

Индексы действующих фактов: `(user_id, account_id, occurred_on, amount)` для кандидатов; `(user_id, occurred_on DESC, id DESC)` и `(user_id, account_id, occurred_on DESC, id DESC)` для списков; `(user_id, refund_of_transaction_id)` для лимита; `(user_id, category_id, occurred_on)` для прямых категорий. Полные индексы обратных FK/истории сохраняются и для отмененных строк. Фильтр эффективной категории refund требует JOIN к оригиналу.

### 6.2. `transfer_links`

Поля: `outgoing_transaction_id UUID NULL`, `incoming_transaction_id UUID NULL`, `confirmation_action_id UUID NN`, `ended_by_action_id UUID NULL`, `cancelled_by_id UUID NULL`. Все FK включают user_id. CHECK хотя бы одной стороны, разных ID при двух сторонах и не более одного способа завершения.

Действующая связь имеет оба признака завершения NULL. Частичные UNIQUE `(user_id, outgoing_transaction_id)` и `(user_id, incoming_transaction_id)` для действующих связей запрещают повтор роли. C-02 проверяет роли между индексами, знак/тип, разные счета, противоположные суммы и ровно одну связь каждой действующей transfer. matched/unmatched вычисляется по числу сторон; даты могут различаться.

Состав сторон и confirmation_action_id неизменяемы. Дополнение/замена пары завершает прежнюю связь и создает новую подтвержденную версию атомарно, сохраняя доказательство. Снятие полной пары переводит обе стороны в unclassified; повторная классификация требует явного решения. Внутренние связи исключаемых фактов получают cancelled_by_id той же отмены и остаются историей. Связь с внешним действующим фактом блокирует отмену. Завершенные версии не занимают активную уникальность.

Смешанные суммы не разбиваются и не образуют неравную пару; обратный перевод — новые факты/связь, не refund к переводу.

## 7. Доказательства решений и планы

### 7.1. `financial_actions` и `financial_action_transactions`

**Реализованная часть 2.2/2.4:** модель FinancialAction в `db/financial_actions.py` и таблица financial_actions содержат все перечисленные ниже поля, включая source_attempt_id с составным FK. Разрешены шесть типов Accounts и import_confirm/import_cancel. С Transactions в 2.5 добавляются financial_action_transactions и остальные типы. Полная семантика C-11 остается в 2.7. Ниже сохранена целевая согласованная схема, без фиктивных таблиц или незащищенных ссылок на будущие модули.

Это ограниченный аудит D-13, **не event store**. Он принадлежит финансовому сценарию, не вводит восьмой бизнес-модуль.

`financial_actions`: `request_id UUID NN`, `action_type TEXT NN`, `plan_version INTEGER NN`, `plan_digest BYTEA NN`, `schema_version SMALLINT NN DEFAULT 1`, `account_id UUID NULL`, `source_attempt_id UUID NULL`, `reason_code TEXT NULL`, `reason TEXT NULL`, `details JSONB NN`.

CHECK положительных версий, digest 32 байта, JSON-объекта details и action_type из: import_confirm, import_cancel, set_kind, set_category, confirm_transfer, replace_transfer, unlink_transfer, link_refund, unlink_refund, confirm_unlinked_refund, confirm_balance, revoke_balance, confirm_history, revoke_history, archive_account, restore_account. UNIQUE `(user_id, id)`, `(user_id, request_id)`; составные FK счета/попытки. created_at действия задается текущими серверными часами при его записи после блокировки, а не временем начала ожидавшей транзакции; user_id — владелец/инициатор из сессии. Других исполнителей в MVP нет.

`financial_action_transactions`: `action_id UUID NN`, `transaction_id UUID NN`, `before_state JSONB NN`, `after_state JSONB NN`. FK владельца действия/операции; UNIQUE `(user_id, action_id, transaction_id)`, CHECK JSON-объектов. При создании before_state — пустой объект. Снимки содержат ревизии, прежние/новые типы, ID связей и конкретное согласие, где оно обязательно. Полные версии прошлых категорий/правил не реконструируются; set_category сохраняет происхождение текущего решения.

Для import_cancel обязательны непустая причина, source_attempt_id, набор insert, сохраненные link и последствия; для transfer — принадлежность счетов себе и выбор сторон; для unlinked refund — отдельное подтверждение смысла/предупреждения; для history — диапазон и просмотренный набор; для опоры — EOD-семантика и введенная сумма. Архив/восстановление сохраняют состояние счета. Автоматический отзыв полноты ссылается на import_confirm/import_cancel и затронутые ID/даты; фиктивного пользовательского действия нет.

Details строит сервис по типизированной схеме, произвольный клиентский JSON не становится доказательством. В details сохраняются нормализованный accepted_plan, по которому вычислен digest (включая решения по совпадениям и показанные различия), и неизменяемый result команды с ID/счетчиками/исходными результатами классификации. Это позволяет вернуть прежний результат после последующих изменений фактов и проверить принятый digest после удаления временного плана. Метаданные отмены добавляются к ответу явно, не переписывая прежний result. Избыточного raw/XLSX в accepted_plan нет. C-11 проверяет тип, владельца, цель, необходимые ссылки/элементы и соответствие изменению. UPDATE/DELETE принятых действий запрещены. БД гарантирует структуру/атомарность, реальное согласие и проверенную сессию обеспечивает сервис.

### 7.2. Временный серверный план

У непринятого импорта preview_plan содержит ошибки файла/строк, предупреждения, состояние полного разбора, кандидатов, предложения и все обязательные действия; счетчики строк/движений проверяются отдельно. Изменение плана повышает preview_version и меняет digest. При принятии/очистке план удаляется после переноса необходимых постоянных решений в movements/actions.

Для коротких действий (отмена, полнота, изменение связи) сервер строит preview из БД и заново строит его при команде, сравнивая версию/digest. Отдельное постоянное хранилище непринятых планов не нужно. Принятый план/доказательства сохраняются атомарно. Digest — контроль актуальности, не авторизация.

Кодирование `financial_plan_v1`: SHA-256 от UTF-8 JSON с отсортированными ключами объектов, фиксированным порядком массивов, компактными разделителями, без BOM/перевода строки; Decimal — строки с точными двумя знаками, даты ISO, UUID в нижнем регистре. Нет float/NaN; отсутствие значения — явный NULL. Это отдельный алгоритм от money_manager_match_v1.

Digest включает владельца, тип команды, ID **конкретной попытки/результата**, версию плана/схемы, mapping и движения, решения/выбранные ID, показанных кандидатов с релевантным состоянием, предлагаемые типы/категории/происхождение и отдельные согласия D-04/D-05. Для отмены — весь набор insert, зависимости/последствия; для полноты — границы и отсортированные ID/счета/даты/суммы действующих фактов диапазона, включая пустой набор. Категории/типы и их revision не меняют digest полноты: состав движений прежний. Текущее время, TTL-остаток и случайный request_id не входят; срок проверяется отдельно. Нерелевантная редакция правила, не меняющая предложение, не меняет план. Исходные планы не попадают в логи.

Повтор request_id с тем же действием/планом возвращает сохраненный результат, иной план — конфликт. Уже принятый Confirm возвращается по attempt_id + сохраненному preview_digest прежде текущих проверок; отмена — по attempt_id + digest ее принятого плана. Отметка отмены видна и после новой попытки; старые команды на нее не переадресуются.

## 8. Межстрочные ограничения, атомарность и конкуренция

### 8.1. Корень блокировки

Согласован протокол MVP: **все финансовые изменения одного владельца сериализуются на его users через FOR NO KEY UPDATE**. Сервис берет корень отдельным SQL-запросом до чтения изменяемого плана, затем — счета по UUID, batch/попытки, категории/правила, операции и связи по UUID. Другие владельцы имеют независимые корни. Проверка сессии и чтение финансов не требуют такой финансовой команды.

Это заменяет прежнее предложение начинать только со счетов: корень покрывает отсутствие операций, новые правила, слоты импорта и заранее неизвестные зависимости отмены. После ожидания все значимое читается заново в READ COMMITTED. Множество счетов определяется под корнем и не расширяется молча после согласия. [Свойства блокировок](https://www.postgresql.org/docs/current/explicit-locking.html).

BEFORE guards финансовых таблиц берут тот же корень до изменения; отложенные проверки выполняют новые чтения после сериализации. Функции таких триггеров — VOLATILE; ожидавший запрос не использует старый снимок для проверки зависимостей ([семантика снимков функций](https://www.postgresql.org/docs/current/xfunc-volatility.html)). Прямой некорректный DML не обходит межстрочные условия; deadlock приводит к rollback. Runtime-транзакция меняет данные одного владельца; служебный обход идет отдельными транзакциями по владельцам. Runtime-роль не владеет схемой, не отключает триггеры/не делает TRUNCATE. Проверки guards/прав — в этапе 2.

### 8.2. Реестр C-01–C-11

Межстрочные условия: целевые constraint triggers AFTER ROW DEFERRABLE INITIALLY DEFERRED; неизменяемость/отзыв полноты — немедленные guards. Межтабличный CHECK не заменяет триггер. Проверяются старые и новые зависимости при смене ссылки, классификации и отмене. [Основание](https://www.postgresql.org/docs/current/sql-createtrigger.html).

| ID | Финальное условие и точки проверки |
| --- | --- |
| C-01 | Связи одного владельца, движения/факты соответствуют счету mapping; составные FK и проверки movements/mappings/transactions |
| C-02 | Действующая transfer имеет ровно одну подтвержденную действующую связь; корректные знак/тип, разные счета, противоположные суммы пары; triggers transactions/transfer_links/actions |
| C-03 | Действующий refund ссылается только на действующий income/expense владельца, знак противоположен, дата не раньше, сумма модулей всех действующих refund <= модулю оригинала; проверка обоих концов при изменениях |
| C-04 | Прямая категория соответствует направлению; linked refund хранит пустую прямую категорию и наследует оригинал, включая архив; старое происхождение rule не сверяется с нынешней целью правила |
| C-05 | Новое прямое назначение/активное правило не использует архивную категорию; архив выключает правила атомарно. Сохранение назначения и наследование linked refund разрешены |
| C-06 | При переходе в accepted требуются полный непустой разбор без ошибок/pending, mapping/результаты, счетчики/число сторон, совпадение digest с import_confirm и сохраненный план/result; актуальность плана заново проверяет сервис. Последующие изменения классификации не сверяются с прежним предложением |
| C-07 | insert взаимно соответствует source_movement и единственному новому факту; link совпадает по владельцу/счету/дате/сумме/RUB, не меняет факт; результаты внутри попытки различны, наблюдения неизменяемы |
| C-08 | Один действующий принятый результат и одна текущая неподтвержденная попытка в batch; новое принятие лишь после отмены прежнего и явного нового разбора; старый attempt/digest неизменяем |
| C-09 | Исключение равно insert отменяемого принятия; внешние действующие наблюдения/связи и еще действующие связи этого Confirm с прежним фактом блокируют отмену. Отмененные наблюдения не блокируют; внутренние связи сохраняются отмененными |
| C-10 | INSERT факта и NULL → cancelled_by_id немедленно отзывают все затронутые интервалы и повышают facts_version в той же транзакции; link/классификация этого не делают, опоры не отзываются |
| C-11 | Факты/происхождение/принятые результаты/действия/отмены неизменяемы в согласованных частях; опоры/полнота только однократно отзываются с причиной; тип/связь/архив имеют доказательство и старое/новое состояние; отмененные факты не оживают |

Историческая ссылка на позднее отмененный факт допустима **лишь в отмененном результате**; новый/действующий принятый link требует действующего факта. При отмене проверяются оба направления refund и все роли переводов. Наличие FK без активного статуса не доказывает допустимость.

### 8.3. Confirm, отмена и чтение

Confirm: сессия → корень/блокировки → конкретная попытка → сначала возврат уже принятого результата по digest → иначе состояние/TTL после ожидания до первой финансовой записи → повторное построение/сравнение полного плана → действие, insert/link, происхождение и отдельно согласованные связи → отзыв полноты новых фактов → accepted и очистка raw/временного плана → ограничения → COMMIT → ответ. Для TTL нужны текущие часы после ожидания, например clock_timestamp(), а не фиксированный на начало транзакции now()/CURRENT_TIMESTAMP ([PostgreSQL](https://www.postgresql.org/docs/current/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)). Если проверка пройдена до срока, commit может завершиться после него по D-09. Link не разрешает переклассифицировать прежний факт; отдельные D-04/D-05 отражаются в плане/аудите. Любая ошибка откатывает всю книгу.

Отмена: конкретный принятый attempt/digest → прежний результат отмены при повторе → под корнем повторная проверка insert, внешних наблюдений/связей и плана → действие/отмена → исключение только этих фактов и внутренних связей → отзыв затронутой полноты без восстановления прежней → COMMIT. Link и исходные значения остаются. All-link без изменения финансовых связей отменяется с нулем исключений.

Полнота/изменение связей используют тот же корень и повторное чтение. Поэтому подтверждение либо предшествует поздней операции и затем отзывается, либо операция уже видна и старое подтверждение отвергается. Два refund не превышают общий лимит; два Confirm не используют устаревших кандидатов.

Повтор после deadlock/serialization failure повторяет **всю** транзакцию и проверку digest. Новый план не подтверждается автоматически. Аналитика/остатки/прогноз читают один read-only REPEATABLE READ snapshot; следующий расчет после commit видит новое состояние. Необъявленного кэша устаревших финансовых итогов нет.

## 9. Изоляция и хранение

user_id берется из сессии, все SELECT/UPDATE/списки/JOIN scoped; чужой ID не раскрывает существование ресурса. Составные FK защищают запись, **не произвольный SELECT**. RLS первоначально не используется (D-03). Финансовые значения, исходные поля и причины не выводятся в технические логи/SQL-ошибки клиенту.

Текущие расчеты/кандидаты используют действующие факты, интервалы и связи. Архив счета/категории не исключает историю. Отмены выделяются явно; полные обратные индексы обслуживают сохраненные ссылки.

Сроки D-09: исходные байты только на разбор; непринятое содержимое — 24 часа и очистка в течение часа при штатной работе; принятые данные/доказательства/отзывы — пока существует учетная запись. Очистка и Confirm используют один корень/блокировку попытки. Повтор принятого Confirm не зависит от TTL. Резервные копии/восстановление — по IMPORT_LIMITS.md, без заявления о готовой реализации.

Изменяемого текущего баланса, ledger, ежедневных агрегатов, расписаний и event store нет. Таблицы сохраненных прогнозов не предлагаются до D-11; это не продуктовый запрет будущего хранения запусков.

## 10. Проверка и приемка

В 0.13 сверены поля/ссылки/жизненные циклы, составлена [матрица инвариантов](FINANCIAL_INVARIANTS.md) и дополнительные сценарии S-01–S-12. Пользователь принял схему и технический пакет 0.13 2026-11-02 в указанном выше объеме. Хранение Auth отдельно уточнено и реализовано в 2.1 от 2026-11-19 (§2); HTTP-параметры остаются перед этапом 3.

Фактически выполнены проверки UTF-8, парности блоков кода, 201 локальной ссылки/якоря в 14 Markdown-документах, последовательностей F-01–F-36, C-01–C-11, T-01–T-37, CAT-01–CAT-25 и S-01–S-12, а также git diff --check. Формулы §10 доменной модели сверены через Decimal по существующему синтетическому expected.json: 6 записей/7 движений, доход 5000.00, чистый расход 910.25, поток 4089.75; равенство движения и компонентов проверено для всех счетов и каждого отдельно. Образец не содержит опоры и не дает фактического остатка; файлы образца не менялись.

В рамках пакета 0.13 SQL, PostgreSQL, миграции и тесты приложения не запускались. Позднее в задаче 1.6 проверены только инфраструктура миграций и ACL (§1). В этапе 2 обязательны проверки предметных DDL/guards на выбранной версии PostgreSQL: прямой DML, две независимые сессии, циклические FK Confirm, обмен приоритетов и сценарии матрицы. Проверка документов не заменяет интеграционные тесты. Технические пакеты 0.13 и [архитектуры 0.14](ARCHITECTURE.md) приняты пользователем 2026-11-02. Этап 0 завершен; задачи этапа 1 выполняются по отдельным поручениям.
