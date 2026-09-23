# Разбор обработки сообщений в локальных Telegram-проектах

**Проекты:**

- `/home/moses/tg_bot_floorball_site`
- `/home/moses/audio_transcription`

**Цель:** определить, что можно переиспользовать в procurement-боте, а что необходимо исправить.

---

## 1. Итог

Лучшей стартовой базой является `audio_transcription`, потому что там уже реализованы:

- конкурентный Telegram ingress с последовательностью внутри одного чата;
- PostgreSQL inbox/jobs/outbox;
- скачивание media вне транзакции;
- локальный Telegram Bot API для больших файлов;
- FFmpeg/FFprobe;
- детерминированное разбиение длинного аудио;
- checkpoint manifest и продолжение provider job после рестарта;
- concurrent workers и продление lease;
- отправка текста и документов через durable outbox;
- AGY provider и HTML/PDF renderer.

Но проект нельзя копировать целиком: платежи, тарифы, AssemblyAI и document-collection домен не нужны. Кроме того, в media intake есть crash-gap между фиксацией update и сохранением downloaded media, а AGY wrapper использует `--dangerously-skip-permissions`.

`tg_bot_floorball_site` лучше использовать как источник архитектурных паттернов:

- версионированные dialogue specs;
- deterministic gap evaluator;
- структурированный LLM patch вместо прямого изменения БД;
- `spec_sha256` и `context_sha256` как защита от stale writes;
- безопасный context gateway;
- sandboxed AGY subprocess;
- approval actions, привязанные к actor и конкретной ревизии;
- invalidation старых approvals после новой ревизии.

---

## 2. Поток сообщения в `tg_bot_floorball_site`

### Этап 1. Long polling

**Entry point:** [`TelegramIngress.run`](/home/moses/tg_bot_floorball_site/src/floorball_bot/telegram.py:218)

**Вход:** Telegram `Update` через `getUpdates`.

**Выход:** последовательный вызов `accept(update)` и продвижение offset.

**Поведение:** асинхронное, но updates обрабатываются по одному.

**Ошибки/retry:** Telegram network/server errors повторяются с exponential backoff и jitter. Падение внутри `accept` не локализовано этим циклом и должно перезапустить supervised process.

**Наблюдаемость:** heartbeat и JSON logs; есть тест, что падение polling останавливает весь supervised unit.

### Этап 2. Durable acceptance

**Entry point:** [`TelegramIngress.accept`](/home/moses/tg_bot_floorball_site/src/floorball_bot/telegram.py:261)

**Вход:** Telegram update.

**Выход:** `processed_updates`, message/session changes, job/outbox.

**Side effects:** всё выполняется внутри одной PostgreSQL-транзакции. Unique `update_id` отсекает повтор.

**Сильная сторона:** бизнес-изменение и outbox атомарны.

**Слабая сторона:** часть media скачивается внутри этой транзакции, что удерживает connection/locks на время сети.

### Этап 3. Routing и авторизация

**Entry point:** [`TelegramIngress._route`](/home/moses/tg_bot_floorball_site/src/floorball_bot/telegram.py:276)

Порядок:

1. callbacks;
2. contact/self-binding;
3. `/start` deep links;
4. active actor и RBAC;
5. special workflows;
6. active dialogue session;
7. запись inbound message;
8. command/media/text route.

Система не разрешает свободному сообщению попасть в LLM без выбранного dialogue mode.

### Этап 4. Message ledger

Входящее сообщение сохраняется в `messages` до постановки AI-задачи: session, user, Telegram chat/message/update IDs, direction, type, original text и media group.

Это хороший шаблон для procurement: Telegram и WhatsApp должны использовать общий ledger, но иметь channel-specific external IDs.

### Этап 5A. Текст

Текст создаёт `extract` job с:

- user ID;
- session ID;
- pinned dialogue mode;
- source text;
- idempotency key из update ID.

Текст не обрабатывается моделью внутри Telegram transaction.

### Этап 5B. Голос

Voice/audio скачивается, затем создаётся `transcribe` job. Worker вызывает speech provider и создаёт следующий `extract` job.

**Проблема:** [`Worker._transcribe`](/home/moses/tg_bot_floorball_site/src/floorball_bot/worker.py:724) находит для результата последнее voice/audio-сообщение по `chat_id`, а job не несёт точный `message_record_id`. При двух близких голосовых сообщениях возможно неверное связывание.

**Исправление для procurement:** payload должен содержать `incoming_message_id` и `attachment_id`; worker обновляет запись по primary key.

### Этап 6. Структурированное извлечение

**Entry point:** [`Worker._extract_dialogue`](/home/moses/tg_bot_floorball_site/src/floorball_bot/worker.py:804)

Порядок:

1. загрузить active session;
2. проверить закреплённый `definition_hash`;
3. загрузить structured memory;
4. построить минимальный context snapshot;
5. записать context hash;
6. вызвать AGY с JSON Schema;
7. проверить `mode/spec_sha256/context_sha256`;
8. применить только allowlisted field patch;
9. обычным кодом вычислить gaps;
10. записать новую memory revision и outbox-вопрос.

Это почти готовый шаблон intake/clarification для закупок.

### Этап 7. AGY subprocess

**Entry point:** [`AgyExtractor`](/home/moses/tg_bot_floorball_site/src/floorball_bot/providers/agy.py:52)

Сильные стороны:

- temporary working directory;
- `--sandbox`;
- slash commands disabled;
- Pydantic JSON Schema;
- trusted policy и user text в JSON envelope как untrusted data;
- one repair attempt;
- ограничение stdout/stderr;
- timeout, TERM, затем KILL process group;
- global semaphore;
- модель возвращает proposal, а не пишет БД.

Слабость: environment allowlist всё ещё содержит `HOME`. Для procurement лучше подставлять отдельный пустой runtime HOME/AGY_HOME с только необходимой конфигурацией.

### Этап 8. Queue/outbox

**Entry points:** [`queue.py`](/home/moses/tg_bot_floorball_site/src/floorball_bot/queue.py:1)

Jobs используют:

- stable SHA-256 idempotency key;
- `pending/running/retry/succeeded/dead`;
- `available_at`;
- attempts/max attempts;
- lease через `locked_at/locked_by`;
- `FOR UPDATE SKIP LOCKED`;
- exponential backoff + jitter.

Outbox создаётся идемпотентно в бизнес-транзакции и доставляется отдельным Telegram loop.

**Пробел:** основной floorball worker не продлевает lease долгих jobs, поэтому очень длинная операция потенциально может быть забрана повторно после lease timeout. В audio-проекте это исправлено.

---

## 3. Поток сообщения в `audio_transcription`

### Этап 1. Параллельный ingress с порядком по чату

**Entry points:**

- [`TelegramIngress.run`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:314)
- [`TelegramIngress._process_update`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:342)

Updates из одного poll обрабатываются через `asyncio.gather`, общий semaphore ограничивает ingress concurrency, а `chat_id → asyncio.Lock` сохраняет порядок сообщений одного чата.

Это лучше floorball-варианта для нескольких пользователей.

Для нескольких экземпляров bot-процесса in-memory lock недостаточен; нужен PostgreSQL advisory lock или partitioning по chat ID.

### Этап 2. Разделение media и обычных updates

**Entry point:** [`TelegramIngress.accept`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:370)

Media сразу направляется в специальный двухфазный путь. Текст/команды обрабатываются одной короткой транзакцией.

### Этап 3. Первая транзакция media intake

**Entry point:** [`TelegramIngress._accept_media_update`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:2280)

В первой транзакции:

- дедуплицируется update ID;
- создаётся/проверяется пользователь;
- проверяется private chat;
- определяется active revision;
- проверяются consent, размер, длительность и число активных записей.

Затем транзакция закрывается.

### Этап 4. Скачивание без DB transaction

**Entry point:** [`TelegramIngress._download_media`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:2420)

Выполняются:

- `getFile`/download;
- private path с mode `0600`;
- проверка фактического размера;
- извлечение audio из video;
- FFprobe duration;
- SHA-256.

Это правильнее скачивания внутри транзакции.

### Этап 5. Вторая транзакция

**Entry point:** [`TelegramIngress._store_prepared_media`](/home/moses/audio_transcription/src/transcription_bot/telegram.py:2473)

Под user row lock повторно проверяются capacity/quota, затем атомарно создаются:

- recording;
- usage reservation;
- `transcribe` job;
- outbox acknowledgement.

Повторная проверка после network I/O закрывает race condition между несколькими файлами одного пользователя.

### Критический crash-gap

После первой транзакции `processed_updates` уже существует, но до второй транзакции нет recording или download job. Если process упадёт во время скачивания, повтор update будет отвергнут как уже принятый, а offset после restart может уйти дальше.

Для procurement первая транзакция должна создавать:

```text
processed_update
message
incoming_attachment(status=accepted)
download_attachment job(file_id, file_unique_id)
ack outbox
```

После этого скачивание делает worker. Тогда нет промежутка, где принятое сообщение существует только в памяти процесса.

### Этап 6. Worker queue

**Entry points:**

- [`queue.py`](/home/moses/audio_transcription/src/transcription_bot/queue.py:1)
- [`Worker.run`](/home/moses/audio_transcription/src/transcription_bot/worker.py:76)
- [`Worker.process`](/home/moses/audio_transcription/src/transcription_bot/worker.py:121)

Особенности:

- configurable concurrency;
- каждый job — отдельный asyncio task;
- lease renewal в background task;
- потеря/завершение job отменяет renew task;
- retryable и permanent errors разделены;
- terminal failure обновляет предметную запись и сообщает пользователю.

Это подходящая база для search, browser, OCR, transcription, report и messaging workers.

### Этап 7. Длинное аудио

**Entry points:**

- [`AudioChunker`](/home/moses/audio_transcription/src/transcription_bot/audio_chunks.py:24)
- [`Worker._prepare_chunk_manifest`](/home/moses/audio_transcription/src/transcription_bot/worker.py:535)
- [`Worker._transcribe_chunks`](/home/moses/audio_transcription/src/transcription_bot/worker.py:586)

Алгоритм:

1. FFprobe получает длительность.
2. Если файл выше provider limit, FFmpeg переводит в mono MP3 16 kHz с фиксированным bitrate.
3. Части получают детерминированные индексы/start offsets.
4. Manifest хранит source SHA-256, параметры и provider ID каждой части.
5. Готовый JSON каждой части пишется атомарно.
6. После рестарта готовые parts читаются с диска, а submitted provider IDs продолжают polling.
7. Таймкоды сдвигаются и результат собирается в исходном порядке.

Для коротких закупочных voice это может быть избыточно, но почти бесплатно поддерживает длинные пояснения и приложенные аудиофайлы.

### Этап 8. Результат и outbox

После транскрибации worker в одной транзакции:

- обновляет recording;
- записывает transcript/provider metadata;
- создаёт message preview;
- создаёт document outbox;
- при необходимости ставит следующий structuring job.

Outbox поддерживает message/document/callback, сохраняет внешний Telegram message ID и повторяет временные ошибки.

### Этап 9. AGY

**Entry point:** [`AgyStructurer`](/home/moses/audio_transcription/src/transcription_bot/providers/agy.py:39)

Полезно:

- JSON Schema;
- Pydantic validation;
- semaphore;
- timeout и process-group termination;
- untrusted transcript envelope;
- большие input limits;
- отдельные prompts для structuring/revision.

Нельзя переносить как есть:

- применяется `--dangerously-skip-permissions`;
- в окружение передаются `HOME`, `AGY_HOME`, иногда API key;
- native audio transcription просит AGY прочитать путь к файлу;
- ошибки JSON в основном классифицируются retryable, хотя часть из них permanent/model-contract.

Для procurement использовать sandboxed wrapper из floorball, а speech provider сделать отдельным адаптером.

---

## 4. Сравнение

| Возможность | Floorball | Audio transcription | Решение для procurement |
|---|---|---|---|
| update dedupe | да | да | использовать |
| per-chat ordering | последовательный весь ingress | lock на chat + global concurrency | взять audio pattern |
| download вне transaction | нет для основного media flow | да | worker-based durable download |
| durable download job до сети | нет | нет | добавить |
| PostgreSQL queue | да | да | использовать общий модуль |
| lease renewal | нет в базовом worker | да | взять audio pattern |
| durable outbox | да | да | использовать |
| versioned dialogue spec | да | нет | взять floorball pattern |
| deterministic missing fields | да | нет | взять floorball pattern |
| context redaction/gateway | да | минимально | взять floorball pattern |
| safe AGY sandbox | лучше | хуже | взять floorball wrapper |
| large Telegram files | PDF до 20 MB | local Bot API до 2 GB | взять audio transport |
| audio chunk/resume | базовая transcription | полноценный manifest | взять audio pattern |
| HTML/PDF renderer | publication-specific | универсальный документ | адаптировать audio renderer |
| explicit approvals | сильная revision-bound модель | callbacks для форматов/правок | взять floorball pattern |

---

## 5. Рекомендуемый procurement message flow

```text
Telegram getUpdates
  → per-chat serialization
  → short transaction:
       processed_update
       inbound message
       attachment metadata
       download job
       acknowledgement outbox
  → commit and advance offset
  → download worker:
       getFile / local Bot API
       size + MIME + SHA-256 + antivirus
       object storage
  → short transaction:
       attachment ready
       extract/transcribe job
  → extraction worker:
       PDF/DOCX/OCR or voice transcript
       immutable source segments
  → intake AGY:
       sandbox + strict JSON Schema
       versioned spec/context hashes
       untrusted content envelope
  → deterministic patch validator
  → deterministic gaps evaluator
  → case/item/clarification update + Telegram outbox
```

После закрытия clarification blockers тот же queue pattern запускает search jobs, browser jobs, WhatsApp dialog jobs и report jobs.

---

## 6. Что переносить первым

1. Объединённый `queue.py`: audio-версия lease renewal + floorball idempotency/audit conventions.
2. Telegram polling supervisor и per-chat locks.
3. Новый crash-safe `incoming_attachments` intake.
4. `AudioChunker` и checkpoint manifest.
5. Floorball dialogue repository/spec/evaluator/patch validator.
6. Floorball sandboxed AGY wrapper с отдельным runtime HOME.
7. Audio HTML/document renderer и Telegram chunk formatting.
8. Health/heartbeats/systemd templates.

Не переносить на первом этапе:

- payments/subscriptions;
- site publisher/Git/Plesk;
- news/media consent domain;
- AssemblyAI key pool;
- collection revision UI целиком;
- существующие огромные `telegram.py` без разделения по handlers.

Для нового проекта Telegram handlers стоит разнести на `commands`, `intake`, `clarifications`, `approvals`, `reports` и `errors`, оставив ingress тонким.

---

## 7. Проверка состояния проектов

Запущены локальные тесты без изменения исходников:

- `tg_bot_floorball_site`: **89 passed, 74 skipped**;
- `audio_transcription`: **88 passed, 2 skipped**.

Большинство пропусков floorball относятся к PostgreSQL/integration-набору без заданной тестовой БД, поэтому результат подтверждает unit/contract слой, но не полную production-интеграцию. Оба worktree имеют незакоммиченные пользовательские изменения; они не изменялись в ходе анализа.

В репозиториях не найден отдельный `LICENSE`. Для внутреннего переиспользования владельцем это организационно просто, но перед распространением нового проекта лицензию нужно определить явно.
