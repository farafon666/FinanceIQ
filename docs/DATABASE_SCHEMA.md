# FinanceIQ — проект PostgreSQL-схемы MVP

**Статус:** проект для согласования, не миграция и не исполняемый DDL.  
**Дата:** 2026-10-27.  
**Основания:** [PRODUCT.md](PRODUCT.md), [DOMAIN_MODEL.md](DOMAIN_MODEL.md), [DECISIONS.md](DECISIONS.md).

Все новые технические решения ниже — предложения. Обязательные исходные требования: PostgreSQL, `NUMERIC(18,2)` для операций, одна валюта, изоляция владельцев, атомарный импорт, Modular Monolith. До выбора D-01–D-13 схема не считается утвержденной.

## 1. Соглашения

Предлагаются 11 таблиц в одной БД, без PostgreSQL extensions, секционирования и отдельных хранилищ модулей. Границы модулей обеспечиваются кодом и ответственностью сервисов.

- Все таблицы имеют `id UUID PRIMARY KEY` и `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. UUID генерируется приложением; отдельная инфраструктура UUID не нужна.
- Все таблицы, кроме `users`, имеют `user_id UUID NOT NULL REFERENCES users(id)`. Эти общие поля не повторяются в таблицах полей ниже.
- Обозначение `NN` — `NOT NULL`, `NULL` — допускает отсутствие значения. Для nullable-полей default — `NULL`; для прочих default отсутствует, если не указан.
- `UNIQUE(user_id, id)` добавляется только там, где является целью составного FK: `accounts`, `categories`, `categorization_rules`, `import_batches`, `import_rows`, `transactions`.
- Все FK — `ON DELETE RESTRICT ON UPDATE RESTRICT`, если явно не указано иное. Каскадное удаление финансовой истории не предлагается. Политика удаления пользователя — D-09/D-13.
- Идентификаторы и владельцы неизменяемы. `created_at` — время записи, `booked_on` — дата банковского проведения, `balance_date` — дата остатка. Они не взаимозаменяемы.
- Даты операций — `DATE`; технические события — `TIMESTAMPTZ`. Календарный период — `[start_date, end_date)`; подтверждение полноты хранит включительные `start_date/end_date`.
- `MVP_CURRENCY`, `APPROVED_FORMAT` и `APPROVED_SOURCE` — **метаобозначения** еще не выбранных констант, не SQL-переменные и не defaults. После согласования они заменяются фиксированными строками в CHECK. Предложение `RUB` не считается утвержденной валютой.
- Строковые статусы — `TEXT` с CHECK перечисления, без PostgreSQL ENUM. Причина: явные ограничения при еще согласуемом наборе значений; изменение потребует обычной миграции, а не отдельного типа.

Для всех денежных полей, включая nullable staging, действует условие конечности:

```text
value IS NULL OR value BETWEEN -9999999999999999.99 AND 9999999999999999.99
```

В основных таблицах NULL дополнительно запрещен. Это исключает `NaN`; бесконечности не помещаются в выбранную точность. Нулевая сумма запрещена только для фактических операций. Агрегаты считаются без приведения результата SUM к `NUMERIC(18,2)`, чтобы сумма множества допустимых операций не переполнила тип отдельной строки.

`NUMERIC(p,s)` округляет вход с лишними дробными знаками до проверки записанного значения; CHECK после приведения не позволяет восстановить исходную дробь. Поэтому точное представление проверяется в Decimal **до** SQL, а не обещается одним типом столбца. См. [PostgreSQL: Numeric Types](https://www.postgresql.org/docs/current/datatype-numeric.html). Доступ на запись имеют только контролируемые серверные пути; тестируется и эта граница.

## 2. Auth

### 2.1. `users`

Владелец: Auth. Решения D-03/D-07/D-12.

| Поле сверх общих `id`, `created_at` | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `email` | `TEXT` | NN | Нормализованный адрес входа |
| `password_hash` | `TEXT` | NN | Самодостаточный хеш пароля с параметрами алгоритма |
| `timezone` | `TEXT` | NN | Явно выбранная IANA-зона, без догадки из окружения разработчика |
| `is_active` | `BOOLEAN` | NN, `true` | Запрет входа без удаления финансовых данных |

Ограничения: `UNIQUE(email)`; CHECK `email = lower(btrim(email))`, `char_length(email) BETWEEN 3 AND 254`; CHECK непустых `password_hash/timezone`. Формат email и существование IANA-зоны проверяет сервис. Нормализация всего email в нижний регистр — согласуемая политика входа D-12, не универсальное утверждение об email.

### 2.2. `auth_sessions`

Владелец: Auth. Предлагаемый серверный вариант; окончательная схема зависит от D-12.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `token_hash` | `BYTEA` | NN | SHA-256 высокоэнтропийного случайного токена сессии |
| `csrf_token_hash` | `BYTEA` | NN | Хеш отдельного CSRF-секрета, привязанного к сессии |
| `expires_at` | `TIMESTAMPTZ` | NN | Абсолютное окончание сессии |
| `revoked_at` | `TIMESTAMPTZ` | NULL | Выход/отзыв |

Ограничения: `UNIQUE(token_hash)`; CHECK `octet_length(token_hash) = 32`, `octet_length(csrf_token_hash) = 32`, `expires_at > created_at`, `revoked_at IS NULL OR revoked_at >= created_at`.

Срок действия проверяется при запросе; CHECK с `now()` для «сессия еще жива» не используется. Cookie и CSRF-защита — обязанности HTTP-слоя/сервиса, не следствие наличия таблицы. Для хеша пароля SHA-256 не предлагается: нужен специализированный password hashing, конкретные параметры выбираются при проектировании Auth.

Индексы: уникальный по `token_hash`; `(user_id, expires_at)` для отзыва/просмотра своих сессий; `(expires_at)` для ограниченной очистки после согласования хранения.

## 3. Accounts

### 3.1. `accounts`

Владелец: Accounts. Решения D-01/D-06/D-13.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `name` | `TEXT` | NN | Отображаемое название |
| `currency_code` | `TEXT` | NN | Единственная согласованная валюта |
| `archived_at` | `TIMESTAMPTZ` | NULL | Скрытие из активного списка без потери истории |

CHECK: `char_length(btrim(name)) BETWEEN 1 AND 100`; `currency_code = MVP_CURRENCY` после подстановки константы; `archived_at IS NULL OR archived_at >= created_at`. Код валюты неизменяем. Номера счетов/карт, реквизиты доступа и рассчитанный баланс не хранятся.

Ключи: PK `id`; `UNIQUE(user_id, id)`. Название не UNIQUE: одинаковое пользовательское название не доказывает один и тот же реальный счет. Риск импорта одной выписки в два разных созданных счета раскрывается в D-08.

Индекс `(user_id, archived_at, id)` обслуживает список своих счетов; составной уникальный индекс обслуживает FK и поиск по владельцу/ID. Архивация не исключает счет из исторической аналитики или общего прогноза автоматически.

### 3.2. `balance_confirmations`

Владелец: Accounts. Остаток на конец дня, D-06.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | `UUID` | NN | Собственный счет |
| `balance_date` | `DATE` | NN | День, после всех операций которого подтверждена сумма |
| `balance` | `NUMERIC(18,2)` | NN | Подтвержденная сумма; ноль и отрицательные значения допустимы |
| `confirmed_at` | `TIMESTAMPTZ` | NN | Время подтверждения пользователем |
| `revoked_at` | `TIMESTAMPTZ` | NULL | Исправленная/отозванная опорная точка |

FK `(user_id, account_id) → accounts(user_id, id)`. CHECK конечности суммы; `confirmed_at >= created_at`; `revoked_at IS NULL OR revoked_at >= confirmed_at`. При вставке сервис передает согласованные технические timestamps; обычно `created_at = confirmed_at`.

Частичный UNIQUE `(user_id, account_id, balance_date) WHERE revoked_at IS NULL`. Он дает одну действующую точку на дату и поддерживает выбор последней даты до cutoff. Отозванные записи хранятся. Дополнительный индекс `(user_id, account_id, balance_date DESC, id)` — просмотр истории всех подтверждений.

Будущая дата, выбор опоры, сравнение с предыдущей и отзыв при исправлении — сервис; исходные поля подтверждения неизменяемы, разрешен только переход `revoked_at: NULL → timestamp`.

### 3.3. `history_confirmations`

Владелец: Accounts. Пользовательское подтверждение полноты, D-06/D-09.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | `UUID` | NN | Собственный счет |
| `start_date` | `DATE` | NN | Первый полный день, включительно |
| `end_date` | `DATE` | NN | Последний полный день, включительно |
| `confirmed_at` | `TIMESTAMPTZ` | NN | Время явного подтверждения пользователем |
| `revoked_at` | `TIMESTAMPTZ` | NULL | Отзыв или изменение набора фактов |

FK `(user_id, account_id) → accounts(user_id, id)`. CHECK `start_date <= end_date`, `confirmed_at >= created_at`, `revoked_at IS NULL OR revoked_at >= confirmed_at`. Частичный UNIQUE `(user_id, account_id, start_date, end_date) WHERE revoked_at IS NULL` устраняет повтор одного заявления. Перекрывающиеся интервалы разрешены и объединяются при расчете; EXCLUDE/GiST и extension для этого не нужны.

Частичный индекс `(user_id, account_id, end_date, start_date) WHERE revoked_at IS NULL` — поиск действующих интервалов и затронутых поздней операцией подтверждений. Неизменяемость и однонаправленный отзыв аналогичны остаткам.

Приложение не выводит полноту из наличия batch. Триггер фактического добавления операции отзывает действующие подтверждения, содержащие `booked_on`; явное новое подтверждение допускается отдельной командой после проверки истории.

## 4. Categories

### 4.1. `categories`

Владелец: Categories. Плоские персональные категории, D-10.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `name` | `TEXT` | NN | Название |
| `kind` | `TEXT` | NN | `income` или `expense` |
| `archived_at` | `TIMESTAMPTZ` | NULL | Запрет новых назначений, сохранение старых |

CHECK `kind IN ('income','expense')`, `char_length(btrim(name)) BETWEEN 1 AND 100`, корректности `archived_at`. `kind` после создания неизменяем; это исключает изменение направления всех связанных операций через редактирование справочника.

Ключи: `UNIQUE(user_id, id)`; частичный уникальный индекс `(user_id, kind, lower(btrim(name))) WHERE archived_at IS NULL`. В одном направлении нельзя создать две активные категории с одинаковым нормализованным названием; архив не блокирует новое название. Глобальных пользовательских категорий с `user_id = NULL` нет. Начальный набор при необходимости создается как собственные копии, состав набора требует согласования.

### 4.2. `categorization_rules`

Владелец: Categories, D-10.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `category_id` | `UUID` | NN | Категория назначения и направление правила |
| `pattern` | `TEXT` | NN | Подстрока описания |
| `priority` | `INTEGER` | NN | Меньшее значение раньше |
| `is_active` | `BOOLEAN` | NN, `true` | Участие в новых назначениях |

FK `(user_id, category_id) → categories(user_id, id)`. CHECK `priority > 0`, `char_length(btrim(pattern)) BETWEEN 1 AND 200`. Тип сопоставления один — `contains`; поле с фиктивными будущими типами не добавляется.

Ключи: `UNIQUE(user_id, id)`; частичный UNIQUE `(user_id, priority) WHERE is_active`. Индекс `(user_id, category_id)` нужен для поиска правил архивируемой категории. Архивация категории отключает ее активные правила в той же DB-транзакции. Активировать правило на архивную категорию нельзя.

Ссылка на правило в операции — происхождение назначения, не полная версия его исторических условий. Изменение правила не меняет категорию уже записанных операций; точный аудит всех прошлых редакций правил в этот MVP не включается.

## 5. Imports

### 5.1. `import_batches`

Владелец: Imports. Решения D-08/D-09.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | `UUID` | NN | Целевой счет |
| `format_code` | `TEXT` | NN | Единственный согласованный формат, без версии парсера в идентичности файла |
| `source_namespace` | `TEXT` | NN | Стабильная область банковского ID выбранного источника |
| `parser_version` | `SMALLINT` | NN | Версия разбора |
| `normalization_version` | `SMALLINT` | NN | Версия fingerprint |
| `file_sha256` | `BYTEA` | NN | SHA-256 исходных байтов |
| `byte_size` | `BIGINT` | NN | Размер до разбора |
| `status` | `TEXT` | NN, `processing` | Состояние процесса |
| `row_count` | `INTEGER` | NN, `0` | Количество логических строк данных |
| `preview_version` | `INTEGER` | NN, `1` | Версия показываемого плана |
| `preview_digest` | `BYTEA` | NULL | SHA-256 плана, решений строк и применяемых правил |
| `expires_at` | `TIMESTAMPTZ` | NN | Окончание срока неподтвержденного preview |
| `confirmed_at` | `TIMESTAMPTZ` | NULL | Атомарно установленный факт Confirm |
| `error_code` | `TEXT` | NULL | Безопасный код ошибки файла, без исходных финансовых значений |

FK `(user_id, account_id) → accounts(user_id, id)`. Ключи: `UNIQUE(user_id, id)`, `UNIQUE(user_id, account_id, id)` для строгих ссылок строк; `UNIQUE(user_id, account_id, format_code, file_sha256)` **для всех состояний**, чтобы одни байты не становились вторым импортом после истечения preview или смены парсера.

CHECK:

- `format_code = APPROVED_FORMAT`, `source_namespace = APPROVED_SOURCE` после согласования;
- `parser_version > 0`, `normalization_version > 0`, `preview_version > 0`;
- `octet_length(file_sha256) = 32`; `preview_digest IS NULL OR octet_length(preview_digest) = 32`;
- `byte_size BETWEEN 1 AND 10485760`, `row_count BETWEEN 0 AND 10000` — предлагаемые лимиты D-09;
- `status IN ('processing','preview','confirmed','rejected','expired','cancelled')`;
- `expires_at > created_at`;
- `(status = 'confirmed') = (confirmed_at IS NOT NULL)` и `confirmed_at IS NULL OR confirmed_at >= created_at`;
- для `status IN ('preview','confirmed')`: `preview_digest IS NOT NULL AND row_count > 0`;
- для `status = 'rejected'`: `error_code IS NOT NULL`.

Состояния:

```text
processing → preview | rejected
preview → confirmed | expired | cancelled
preview → preview с увеличением preview_version при изменении плана
rejected | expired | cancelled → processing только при явной повторной загрузке
confirmed → терминальное состояние
```

Повторная загрузка возвращает тот же batch по уникальному ключу. Для незавершенного/ошибочного batch допустим повторный разбор после проверки его состояния; подтвержденный не переоткрывается и не перепарсивается. Восстановление зависшего `processing` выполняется явно после проверки, без фоновой очереди. При первом разборе работа синхронная, `processing` не означает Celery/job-сервис.

Индексы: `(user_id, created_at DESC, id DESC)` для истории; `(user_id, account_id, id)` уже есть как UNIQUE; частичный `(expires_at) WHERE status = 'preview'` для истечения. Уникальность файла поддерживается собственным индексом.

### 5.2. `import_rows`

Владелец: Imports. Данные staging допускают ошибки, но не попадают в финансы до Confirm.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `batch_id` | `UUID` | NN | Batch |
| `account_id` | `UUID` | NN | Счет для составных FK, не независимый выбор строки |
| `row_number` | `INTEGER` | NN | Номер логической CSV-записи без заголовка, с 1; не физическая строка при многострочных ячейках |
| `raw_payload` | `JSONB` | NULL | Временные исходные поля для объяснения ошибок; не оригинальный CSV-файл |
| `booked_on` | `DATE` | NULL | Проверенная дата или NULL при ошибке |
| `amount` | `NUMERIC(18,2)` | NULL | Проверенная точная сумма или NULL при ошибке |
| `description` | `TEXT` | NULL | Проверенное описание; пустое допустимо, если формат его не дает |
| `bank_transaction_id` | `TEXT` | NULL | Банковский ID, если источник гарантирует его смысл |
| `row_fingerprint` | `BYTEA` | NULL | Fingerprint нормализованных исходных реквизитов |
| `validation_status` | `TEXT` | NN | `valid` или `invalid` |
| `validation_errors` | `JSONB` | NN, `[]` | Массив безопасных структурированных ошибок |
| `match_status` | `TEXT` | NN, `none` | `none`, `exact`, `ambiguous`, `conflict` |
| `decision` | `TEXT` | NN, `pending` | `pending`, `insert`, `link` |
| `resolved_transaction_id` | `UUID` | NULL | При `link` существующий факт; после Confirm при `insert` созданный факт |
| `proposed_kind` | `TEXT` | NN, `unclassified` | Предлагаемый экономический тип |
| `proposed_category_id` | `UUID` | NULL | Категория для новой операции |
| `proposed_category_origin` | `TEXT` | NN, `none` | `none`, `rule`, `manual` |
| `proposed_rule_id` | `UUID` | NULL | Правило назначения |

FK:

- `(user_id, account_id, batch_id) → import_batches(user_id, account_id, id)`;
- `(user_id, account_id, resolved_transaction_id) → transactions(user_id, account_id, id)`;
- `(user_id, proposed_category_id) → categories(user_id, id)`;
- `(user_id, proposed_rule_id) → categorization_rules(user_id, id)`.

У optional FK используется обычный `MATCH SIMPLE`: пустой ID означает отсутствие связи, `user_id/account_id` при этом остаются NN. Ссылки на родителя другого владельца не допускаются.

Ключи: `UNIQUE(user_id, id)`, `UNIQUE(user_id, account_id, id)` для происхождения операции; `UNIQUE(user_id, batch_id, row_number)`. Число строк ограничено также CHECK `row_number BETWEEN 1 AND 10000` и сервисом; счетчик batch сверяется при завершении разбора.

CHECK перечисленных статусов; JSON-массив для `validation_errors`; `raw_payload IS NULL OR jsonb_typeof(raw_payload) = 'object'`; длина description `<= 2000`; непустой банковский ID длиной `<= 256`, если указан; длина fingerprint 32, если указан; конечность суммы.

Дополнительные строковые условия:

- `valid` требует `NOT NULL` для `booked_on/amount/description/row_fingerprint`, `amount <> 0` и пустой массив ошибок. Пустая строка description допустима. Неизвестную/лишнюю дробь нельзя предварительно округлить ради этого состояния.
- `invalid` требует непустой массив ошибок; `decision = 'pending'`, `resolved_transaction_id IS NULL`.
- `conflict` требует `decision = 'pending'`; блокирует Confirm.
- `pending` требует пустую ссылку результата; `link` — непустую; `insert` допускает пустую ссылку до Confirm и требует ее после Confirm по межтабличной проверке.
- `proposed_kind` имеет тот же перечень и знаковые ограничения, что `transactions.kind`, когда строка valid.
- Категория/источник/правило имеют ту же локальную согласованность, что в `transactions`, для valid-строки. Проверка направления категории — межтабличная.

Новый `transfer` из импорта создает односторонний `transfer_links`; новый `refund` первоначально несвязанный. Связывание с другими операциями — явная команда Transactions после импорта. Предварительная автокатегоризация предлагается для `income/expense`. Для `link` предложенная категория не перезаписывает существующий факт; это явно отражается в preview.

Список кандидатов вычисляется запросом по операциям и строкам batch; отдельная таблица всех кандидатов не требуется. Изменение выбранного кандидата/типа/категории увеличивает `preview_version`, меняет digest и требует Confirm соответствующего preview.

Индексы: `(user_id, batch_id, row_number)` уже есть; `(user_id, account_id, resolved_transaction_id)` для обратных ссылок и FK; `(user_id, proposed_category_id)` и `(user_id, proposed_rule_id)` для ссылок. Fingerprint строк внутри ограниченного batch группируется при разборе, отдельный глобальный индекс staging не предлагается.

## 6. Transactions

### 6.1. `transactions`

Владелец: Transactions. Только подтвержденные фактические операции. Все операции первого MVP имеют происхождение из CSV; ручное создание финансовых фактов не добавляется.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `account_id` | `UUID` | NN | Счет движения |
| `booked_on` | `DATE` | NN | Дата проведения |
| `amount` | `NUMERIC(18,2)` | NN | Подписанная сумма |
| `description` | `TEXT` | NN | Исходное нормализованное описание |
| `kind` | `TEXT` | NN, `unclassified` | Экономический тип |
| `category_id` | `UUID` | NULL | Явно назначенная категория |
| `category_origin` | `TEXT` | NN, `none` | `none`, `rule`, `manual` |
| `categorization_rule_id` | `UUID` | NULL | Происхождение назначения |
| `refund_of_transaction_id` | `UUID` | NULL | Оригинал возврата |
| `source_import_row_id` | `UUID` | NN | Единственная исходная строка |
| `source_namespace` | `TEXT` | NN | Стабильная область банковского ID |
| `bank_transaction_id` | `TEXT` | NULL | Неизменяемый банковский ID |
| `source_fingerprint` | `BYTEA` | NN | Fingerprint исходных полей, без категории/экономического типа |
| `fingerprint_version` | `SMALLINT` | NN | Версия нормализации fingerprint |

FK:

- `(user_id, account_id) → accounts(user_id, id)`;
- `(user_id, category_id) → categories(user_id, id)`;
- `(user_id, categorization_rule_id) → categorization_rules(user_id, id)`;
- `(user_id, refund_of_transaction_id) → transactions(user_id, id)`;
- `(user_id, account_id, source_import_row_id) → import_rows(user_id, account_id, id)`.

Ключи: PK `id`; `UNIQUE(user_id, id)`; `UNIQUE(user_id, account_id, id)`; `UNIQUE(source_import_row_id)` — одна исходная строка не может породить две операции. Несколько других строк могут указывать на этот факт как дубли.

Локальные CHECK:

```text
kind IN ('income','expense','transfer','refund','unclassified')
amount <> 0 AND amount конечна
(kind <> 'income' OR amount > 0)
(kind <> 'expense' OR amount < 0)
refund_of_transaction_id IS NULL OR
  (kind = 'refund' AND refund_of_transaction_id <> id)
category_origin IN ('none','rule','manual')
category_origin = 'none'   → category_id IS NULL AND categorization_rule_id IS NULL
category_origin = 'rule'   → category_id IS NOT NULL AND categorization_rule_id IS NOT NULL
category_origin = 'manual' → categorization_rule_id IS NULL
kind IN ('transfer','unclassified') → category_origin = 'none'
refund_of_transaction_id IS NOT NULL → category_origin = 'none'
kind = 'refund' → category_origin <> 'rule'
```

Стрелки здесь — логические импликации для будущего CHECK (`NOT условие OR следствие`), не синтаксис SQL. В выражениях применяются `IS NULL/IS NOT NULL`, а не `= NULL`, чтобы SQL NULL не обходил проверку.

Также: `char_length(description) <= 2000`; банковский ID NULL или непустой до 256 символов; `source_namespace = APPROVED_SOURCE`; `octet_length(source_fingerprint) = 32`; `fingerprint_version > 0`.

**Уникальность банковского ID:** частичный UNIQUE `(user_id, account_id, source_namespace, bank_transaction_id) WHERE bank_transaction_id IS NOT NULL`. Версия parser/fingerprint в этот ключ не входит. Она не меняет банковскую идентичность.

**Fingerprint:** не UNIQUE. Индекс `(user_id, account_id, fingerprint_version, source_fingerprint)` только ищет кандидатов. При смене алгоритма сервис должен уметь построить кандидатные ключи для ранее сохраненных версий; до этого смена версии блокируется. Исторические fingerprints не пересчитываются молча.

Индексы для основных запросов:

| Индекс | Назначение |
| --- | --- |
| `(user_id, booked_on DESC, id DESC)` | Период и keyset-пагинация всех собственных операций |
| `(user_id, account_id, booked_on DESC, id DESC)` | Операции/баланс конкретного счета |
| `(user_id, category_id, booked_on DESC, id DESC) WHERE category_id IS NOT NULL` | Фильтр категории и обратные ссылки |
| `(user_id, refund_of_transaction_id) WHERE refund_of_transaction_id IS NOT NULL` | Лимит и категория связанных возвратов |
| `(user_id, categorization_rule_id) WHERE categorization_rule_id IS NOT NULL` | Происхождение категорий |

Дополнительный индекс только по `kind`, JSON, полнотекстовый поиск и агрегатные таблицы без измеренного запроса не предлагаются. Наследуемые категории возвратов учитываются JOIN/отдельной веткой агрегации; одного фильтра по `transactions.category_id` для них недостаточно.

### 6.2. `transfer_links`

Владелец: Transactions, D-04.

| Поле сверх общих | Тип | NULL / default | Назначение |
| --- | --- | --- | --- |
| `outgoing_transaction_id` | `UUID` | NULL | Отрицательная сторона |
| `incoming_transaction_id` | `UUID` | NULL | Положительная сторона |

Два FK `(user_id, outgoing_transaction_id)` и `(user_id, incoming_transaction_id)` ссылаются на `transactions(user_id, id)`.

CHECK: хотя бы один ID непустой; если оба непустые, они различны. `UNIQUE(user_id, outgoing_transaction_id)` и `UNIQUE(user_id, incoming_transaction_id)` предотвращают повтор стороны в одной роли. Обычный UNIQUE допускает несколько NULL, поэтому много односторонних переводов разрешено.

Запрет использования одной операции в противоположных ролях, знаки, тип, разные счета, равные суммы и обязательность связи для каждого `transfer` проверяются межстрочными ограничениями ниже. Сумма/дата/счета в `transfer_links` не дублируются. Статус `matched/unmatched` вычисляется по заполненности двух FK.

## 7. Реестр межтабличных инвариантов

CHECK может проверять текущую строку, но не устойчивую согласованность других строк. Для переводов, лимитов возврата и итогового состояния импорта предлагаются **constraint triggers `AFTER ROW DEFERRABLE INITIALLY DEFERRED`** плюс блокировки общих родительских записей. Это позволяет собрать несколько связанных записей в одной транзакции и проверить результат перед COMMIT. См. [PostgreSQL: Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html) и [CREATE TRIGGER](https://www.postgresql.org/docs/current/sql-createtrigger.html).

Сервисы выполняют те же проверки заранее для понятных ошибок; DB-защита не заменяется только проверкой Python.

| ID | Инвариант | Где срабатывает / механизм |
| --- | --- | --- |
| C-01 | Владельцы всех связей совпадают, источник импорта того же счета | Составные FK, включая ссылки строки на batch и transaction |
| C-02 | У каждой `transfer` ровно одна связь; связанные операции имеют нужный знак/тип, полная пара равна по модулю и имеет разные счета | Отложенная проверка на INSERT/UPDATE/DELETE в `transactions` и `transfer_links`, проверяет старые и новые связи |
| C-03 | Оригинал возврата — `income/expense`, знак противоположен, дата возврата не раньше оригинала, сумма всех связанных возвратов не больше оригинала | Отложенная проверка на INSERT/UPDATE/DELETE возврата и UPDATE/DELETE оригинала; оригинал блокируется до изменения |
| C-04 | Категория соответствует направлению: доход/отрицательный refund → `income`, расход/положительный refund → `expense`; linked refund наследует оригинал | CHECK для локальной формы + отложенная проверка категории операции; направление категории неизменяемо |
| C-05 | Новое назначение не использует архивную категорию; активное правило не ссылается на архив | Сервис и проверка на записи назначения/правила/архивации категории. Старые назначения остаются допустимыми |
| C-06 | В `confirmed` batch все строки valid, без conflict/pending, имеют результат; row_count равен числу строк | Отложенная проверка batch и измененных строк |
| C-07 | Для `insert` результат имеет `source_import_row_id = row.id`; для `link` указан уже выбранный совпадающий факт того же счета | Отложенная проверка строк/операций; внутри одного batch два одинаковых банковских ID блокируются до Confirm |
| C-08 | У каждой операции исходная строка в confirmed batch, с совпадающими счетом, датой, суммой, описанием, ID, источником и fingerprint | Отложенная проверка операций, исходных строк и batch; нельзя создать факт из preview |
| C-09 | Фактические исходные поля и подтвержденные результаты импорта неизменяемы | BEFORE UPDATE/DELETE guard. Для операций допускаются только изменения kind/категории/связи возврата с перепроверкой C-02–C-04; для confirmed-строки разрешена только согласованная очистка raw_payload |
| C-10 | Новая операция внутри действующего подтверждения полноты отзывает его атомарно | Запись операции под блокировкой счета + UPDATE соответствующих `history_confirmations.revoked_at` |
| C-11 | Остатки/подтверждения полноты не редактируются, отзыв однократный; технические идентификаторы/владельцы неизменяемы | BEFORE UPDATE/DELETE guard; исключение на очистку ограничивается заранее согласованной политикой D-09 |

Для C-07 решение `link` без банковского ID разрешено только после сравнения исходных полей и явного выбора; fingerprint сам по себе не является доказательством. Экономическая классификация уже существующего результата не переписывается. При наличии одинакового банковского ID с разными исходными полями link блокируется как конфликт источника.

Проверки должны охватывать **обе стороны изменения**, включая снятие связи/переклассификацию оригинала, а не только вставку новой записи. Сервисный путь удаления фактов не предоставляется; guards и RESTRICT защищают от обхода через обычную роль приложения. Специальный административный SQL, отключающий ограничения, не считается поддерживаемым путем записи.

## 8. Атомарность и конкуренция

**Предложение D-02/D-08:** запись работает в `READ COMMITTED` с явными блокировками и повторными проверками после ожидания. Аналитика/прогноз читают один согласованный snapshot в read-only `REPEATABLE READ`.

Общий порядок блокировок для сервисных изменений: собственные `accounts` по возрастанию UUID → затронутые batch → используемые категории/правила по UUID → исходные операции возвратов/переводов по UUID → строки и связи. Сервис заранее определяет все затронутые счета. Назначение категории берет блокировку категории `FOR SHARE`, ее архивирование — конфликтующую блокировку записи; нельзя назначить архив одновременно с его проверкой. Guards/триггеры также блокируют соответствующие корни перед финансовой записью, чтобы конкурентный обход сервисной проверки не нарушал ограничения. Ожидание блокировки завершается повторным чтением, а не использованием старых кандидатов.

Блокировка счета сериализует Confirm двух выписок одного счета даже при отсутствии банковского ID и UNIQUE fingerprint. Блокировка исходной операции сериализует суммарный лимит возвратов с разных счетов. Проверка после получения блокировки обязательна. Deadlock/serialization failure приводит к откату и ограниченному повтору всей DB-транзакции, без частичного продолжения. Механика и порядок блокировок сверены с [PostgreSQL: Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html).

Протокол Confirm:

1. Проверить сессию и собственный счет, начать DB-транзакцию, взять блокировки.
2. Прочитать batch заново. Для `confirmed` с тем же digest вернуть сохраненные результаты строк. Для другого digest вернуть конфликт. Для истекшего/отмененного/не-preview отказать.
3. Сверить `preview_version/digest` из запроса. Повторно проверить строковые ошибки, источники, банковские ID, fingerprint-кандидатов, экономические решения и применяемые категории/правила. Полный набор активных правил читается одним запросом, план фиксируется для текущего Confirm; новое правило, появившееся после этого среза, относится к следующим назначениям. Изменение до повторной проверки, влияющее на план, требует нового preview.
4. Для каждой `insert` создать ровно одну операцию со ссылкой на исходную строку; для нового transfer создать одностороннюю связь; для `link` не создавать операцию.
5. Проставить ссылки результата всех строк. Новые факты отзывают затронутую полноту по C-10.
6. Установить `confirmed_at/status` batch. Все отложенные ограничения должны выполниться на COMMIT. При ошибке откатить весь набор.
7. Отдать итог из сохраненных данных. Никаких «создано» до успешного COMMIT.

Digest строится по каноническому представлению идентификатора batch, версии preview, исходных полей строк, решений, выбранных результатов и назначений категорий/правил. Он не содержит сессионных секретов и не служит авторизацией. Истекший preview нельзя подтвердить, даже если digest совпадает.

## 9. Изоляция чтения и RLS

**Требование:** изоляция на сервере. **Предложение D-03:** для первого этапа обязательны scoped repositories и составные FK; RLS остается отдельным вариантом для согласования. Данная схема не заявляет DB-защиту произвольного SELECT при выключенном RLS.

Обязательные свойства backend: `user_id` берется из сессии; все get/list/update и JOIN фильтруются по нему; чужой ID не отличается наружу от недоступного ресурса; исходные SQL-ошибки/значения не выдаются клиенту; расчеты и история импортов тоже scoped. Роль приложения не имеет прав менять схему/отключать guards; credentials не попадают в документацию и логи.

Если выбран RLS: требуется отдельная runtime-роль без superuser/BYPASSRLS, отделенная от владельца миграций; политики `USING/WITH CHECK` для `user_id` (для `users` — `id`); контекст пользователя только через транзакционный `SET LOCAL` после аутентификации; отсутствие контекста закрывает доступ. Bootstrap входа/поиска сессии проектируется отдельно, без глобальной открытой политики. Обязательно тестирование повторного использования соединения разными пользователями. Владельцы таблиц обычно обходят RLS, FK/UNIQUE также требуют отдельного внимания к ошибкам. См. [PostgreSQL: Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

## 10. Хранение и отсутствие лишних таблиц

Предложение D-09: исходные байты CSV существуют только во время ограниченного разбора; постоянной таблицы/объектного хранилища файлов нет. `raw_payload` может сохраняться до окончания preview, затем очищается. После Confirm сохраняются batch, нормализованные строки, принятые решения, связи и финансовые факты. Без них нельзя объяснить повторный импорт. Очистка raw не меняет digest, поскольку raw не входит в подтвержденную финансовую идентичность.

Не добавляются таблицы `account_balances` с изменяемым балансом, ledger двойной записи, платежные расписания без источника, сохраненные прогнозы, ежедневные агрегаты, универсальный event store, валютные курсы и будущие форматы CSV. Аналитика и прогноз получают необходимые данные из перечисленных таблиц; ограничения воспроизводимости после изменения данных раскрыты в доменной модели.

## 11. Проверка этого проекта

Схема описана на уровне полей, типов, NULL/default, PK/FK/UNIQUE/CHECK, индексов и механизмов межстрочных ограничений. Сценарии T-01–T-33 находятся в [DOMAIN_MODEL.md](DOMAIN_MODEL.md). Перед реализацией необходим выбор открытых вариантов; после реализации — pytest/integration на настоящем PostgreSQL, включая конкуренцию и прямое нарушение ограничений, а не только SQLite.

На этапе проектирования SQL не исполнялся, миграции и тесты приложения не создавались. Технические свойства ограничений сверены с официальной документацией PostgreSQL; это не заменяет будущую проверку DDL и триггеров на выбранной версии сервера.
