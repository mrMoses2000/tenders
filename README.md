# Procurement Agent Bot — durable procurement workflow foundation

Передача следующему агенту: [история работы и оставшиеся задачи](docs/AGENT_HANDOFF_2026-09-23.md).
Текущий план выпуска и фактическая проверка: [production implementation plan, 29.09.2026](docs/production-implementation-plan-2026-09-28.md).

Рабочее ядро агента закупок:

`Telegram → durable PostgreSQL ingress → PDF/DOCX/voice → safe AGY intake → clarification → research plan/evidence → supplier/offers registry → approval-gated WhatsApp boundary → evaluation/HTML report`.

Владелец заявки общается с системой в Telegram, поставщики — через WhatsApp. WhatsApp
**выключен по умолчанию**. Даже при включённом WAHA отправка возможна только для точного
payload, который был отдельно одобрен и атомарно consumed вместе с outbox-событием.

## Что уже реализовано

- Telegram: текст, location, PDF, DOCX, voice, audio, photo metadata;
- атомарная фиксация update/message/attachment/job/outbox в PostgreSQL;
- content-addressed private file storage и проверка PDF/DOCX/image signatures;
- native extraction PDF/DOCX и расшифровка голосовых через AssemblyAI с определением RU/KK;
- AGY в sandbox: строгая JSON Schema, timeout, process-group cleanup, ограниченное окружение,
  prompt-injection boundary и stale-context hashes;
- обязательное уточнение города и географии поиска; неоднозначные данные блокируют поиск;
- детерминированный dialogue router перед intake: обычный разговор и вопросы о статусе не
  могут создать/изменить заявку, а решение по каждому ходу сохраняется в `dialogue_turns`;
- контекст уточнения состоит из текущей нормализованной заявки и ровно одного вопроса,
  фактически показанного пользователю; модель не выбирает действие и не получает SQL-доступ;
- versioned normalized request, items, requirements, clarifications, workflow/audit foundation;
- persistent approval gate keyed by an immutable action payload hash;
- job/outbox leases, retries, exponential backoff, `SKIP LOCKED`, idempotency keys;
- channel-aware Telegram/WhatsApp outbox и health snapshot;
- воспроизводимый research plan: собственная БД, 2GIS и web, включая отдельное разрешение
  разговорных географических обозначений вроде «Первая Алматы»;
- Telegram approval-кнопки с ownership/expiry-проверкой и атомарный переход
  `ready → researching`; кнопка запуска не показывается, пока live executor не подключён;
- строгий read-only browser contract с hash-pinned input/output и запретом выдавать карточку
  бизнеса за подтверждение товара, цены или наличия;
- опциональный live AGY browser worker с отдельным HOME, ровно одним pinned MCP, запретом
  terminal/filesystem tools, ограничением времени/вывода и хешем принятого structured payload;
- PostgreSQL registry для suppliers, aliases, contacts, locations, source evidence, offers и
  датированных offer observations с collision-safe дедупликацией;
- безопасный WAHA adapter, deterministic availability → spec → price dialogue, quiet hours,
  лимиты, opt-out и запрет разглашения внутренней/тендерной цены;
- append-only WhatsApp conversation/message ledger и идемпотентный webhook inbox (пока без
  доверия к содержимому ответа);
- аутентифицированный `POST /webhooks/waha`: отдельный shared secret, 256 KiB limit,
  strict event schema, quarantine неизвестных отправителей и немедленный opt-out;
- детерминированная обработка supplier reply: явные наличие/отсутствие/цена сохраняются как
  evidence, неоднозначный ответ эскалируется владельцу; следующий текст получает отдельные
  Telegram-кнопки «Отправить в WhatsApp / Отмена» и без approval не уходит;
- Mini App показывает точные тексты и получателей первых сообщений поставщикам:
  владелец может выбрать до десяти черновиков, включая истёкшие, и одобрить их
  одним действием; каждое сообщение по-прежнему получает собственный approval
  и проходит существующие проверки перед WAHA;
- каждый supplier conversation связан с точными offer/request-item targets: ответ продавца
  больше не приписывается «единственному похожему офферу» эвристически;
- append-only approval history; WhatsApp outbox содержит обязательный FK на consumed grant и
  повторно сверяет payload hash непосредственно перед сетевой отправкой;
- delivery-time WhatsApp guard повторно проверяет opt-out, состояние, quiet hours, лимиты,
  контакт и порядок событий; sent outbox и outbound ledger фиксируются одной транзакцией;
- предварительное резервирование WAHA message ID и его повторное использование при retry;
- технический hard-gate перед ценовым ранжированием, безопасный статический HTML renderer
  и versioned content-addressed registry; готовый HTML отправляется владельцу как Telegram-файл;
- Decimal-only сравнение подтверждённой стоимости товара на всю партию с учётом
  фасовки, MOQ и кратности заказа; доставка учитывается только по внесённому
  факту, НДС отдельно не рассчитывается;
- read-only вопросы по заявкам на русском/казахском (`сколько позиций не закрыто`, статус,
  приёмка предприятием, предварительная маржа) исполняются только через типизированные SQL;
- evidence-backed учёт: предприятие-получатель, чек/накладная с несколькими позициями,
  фактические закупки, водители/службы доставки и точное распределение общей доставки;
- заявление «закрыл позицию N» создаёт ожидание чека/накладной, но не меняет статус `closed`;
- Mini App позволяет внести фактическую закупку с ценой и поставщиком, связать её с
  предложением и файлом из Telegram либо явно пометить как запись пользователя;
  закупка больше не закрывает позицию сама по себе;
- закрытие заявки в Mini App сверяет принятие/отмену каждой позиции, закупку,
  передачу товара, завершённые рейсы и фактическую доставочную стоимость;
  причины незакрытого остатка показаны рядом с кнопкой закрытия, а повторное
  открытие требует причину и сохраняется в журнале;
- AssemblyAI voice-транскрипция автоматически различает `ru/kk`; низкая уверенность блокирует
  автоматические действия и требует текстового подтверждения;
- supplier-dialogue имеет отдельные русские и казахские шаблоны и хранит язык разговора.

Фото сохраняется безопасно, но OCR пока выключен: бот попросит продублировать данные текстом,
документом или голосом.

## Локальный запуск

### Telegram Mini App

Мобильный кабинет заявок, позиций и связанных поставщиков работает
поверх той же PostgreSQL. Актуальный HTTPS-адрес открывается через кнопку
«Заявки» в меню бота. Запуск: `.venv/bin/procurement-bot miniapp`;
локальная проверка: `http://127.0.0.1:8082/api/health`. Для открытия внутри
Telegram настройте HTTPS reverse proxy к `127.0.0.1:8082`, запишите его URL
в `MINI_APP_PUBLIC_URL` и перезапустите бот. На текущем сервере временный
`procurement-mini-app-tunnel.service` публикует Mini App через Cloudflare,
проверяет новый адрес и обновляет кнопку бота после смены домена. Он включён
в пользовательском systemd вместе с `procurement-mini-app.service`; для пользователя
`moses` включён `linger`, поэтому оба сервиса стартуют после загрузки Ubuntu без
входа в аккаунт. Скрипт `scripts/run_mini_app_tunnel.py` перезапускает туннель,
если внешний health check трижды подряд не проходит или старт застрял более чем
на 120 секунд. Быстрый Cloudflare Tunnel выдаёт новый адрес после перезапуска:
бот получает его автоматически, но старые прямые ссылки на домен перестают работать.
Проверка автозапуска: `loginctl show-user moses -p Linger` и
`systemctl --user is-enabled procurement-mini-app.service procurement-mini-app-tunnel.service`.
Интерфейс фильтрует заявки по
статусу, городу и тексту, а поставщиков — по названию. Подробности и модель
связи с Notion: [docs/mini-app-architecture.md](docs/mini-app-architecture.md).

Требуются Python 3.12+, PostgreSQL, Docker Compose, авторизованный `agy`, Telegram bot token,
`ffmpeg` и AssemblyAI API key во внешнем приватном `.env`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
# Заполнить POSTGRES_DSN и TELEGRAM_BOT_TOKEN. Если numeric Telegram ID
# неизвестен, укажите точный TELEGRAM_BOOTSTRAP_USERNAMES=@username.

.venv/bin/procurement-bot migrate
.venv/bin/procurement-bot health
.venv/bin/procurement-bot run
```

Для локальной установки Moses уже предусмотрен идемпотентный bootstrap: он сохраняет
существующий Telegram token, генерирует отдельные WAHA secrets, включает pinned
`@playwright/mcp@0.0.82`, NOWEB и webhook, не печатая секреты:

```bash
.venv/bin/python -m procurement_bot.local_bootstrap configure \
  --root /home/moses/tenders --telegram-username JustMoses0002
sudo docker compose -f deploy/waha/compose.yaml up -d
.venv/bin/procurement-bot migrate
systemctl --user link deploy/systemd-user/procurement-agent-bot.service
systemctl --user link deploy/systemd-user/procurement-agent-worker.service
systemctl --user link deploy/systemd-user/procurement-waha-webhook.service
systemctl --user enable --now procurement-agent-bot.service \
  procurement-agent-worker.service procurement-waha-webhook.service
```

В systemd polling бота, очередь и WAHA webhook работают отдельными процессами.
Только worker запускает AGY/Chrome; для него отключён `PrivateTmp`, иначе
Chrome на текущем хосте не запускает sandbox helper. `NoNewPrivileges`
остаётся включённым.

`TELEGRAM_BOOTSTRAP_USERNAMES` доверяется ровно до первого совпавшего private message.
Тогда PostgreSQL атомарно закрепляет numeric Telegram ID; последующие проверки используют ID,
а не изменяемый username. Telegram Bot API не позволяет безопасно определить ID владельца по
телефонному номеру.

Голосовые сообщения не обрабатываются локальной моделью. Worker читает только точную
переменную `ASSEMBLI_AI_5` из `/home/moses/audio_transcription/.env`, передаёт аудио в
AssemblyAI, включает автоматическое определение только русского/казахского и сохраняет
полученный язык с confidence. Сам ключ не копируется в `.env` проекта и не передаётся AGY.

Для раздельного масштабирования:

```bash
.venv/bin/procurement-bot bot
.venv/bin/procurement-bot worker
.venv/bin/procurement-bot webhook
```

`bot` только принимает updates и быстро фиксирует их в БД. Скачивание файлов, AGY,
транскрипция и отправка outbox выполняются процессом `worker`.
`webhook` поднимает только WAHA ingress на configured bind address. В локальном Compose он
слушает только gateway выделенного bridge `172.29.247.1`; этот адрес недоступен из LAN, но
доступен контейнеру WAHA. Команда `run` запускает webhook вместе с остальными процессами
только при `WAHA_WEBHOOK_ENABLED=true`.

## Настройки и безопасность

Все параметры перечислены в [.env.example](/home/moses/tenders/.env.example). Файлы хранятся
под `MEDIA_ROOT` с приватными правами. Секреты PostgreSQL, Telegram и будущего WAHA не
передаются subprocess AGY. Intake запускает AGY с отдельным `AGY_HOME`, куда атомарно
копируется только OAuth-токен; глобальные MCP и пользовательские skills не наследуются.
Внешний поиск и WhatsApp запускаются только после детерминированного статуса `ready` и
отдельного approval gate. Парсер AGY и browser research используют разные профили и не
разделяют MCP/tool permissions. Live research включается только через
`BROWSER_RESEARCH_ENABLED=true`, точный `AGY_BROWSER_MCP_SERVER_JSON` и отдельный browser
HOME, который runtime создаёт с allowlist одного MCP. Конфигурацию MCP следует закреплять на
проверенной версии, а не оставлять плавающий `latest`.
При `CODEX_2GIS_FALLBACK_ENABLED=true` только блокировка доступа в ветке 2GIS
переключает поиск с AGY на серверный Codex CLI с Chrome-плагином. Результат
проходит ту же проверку источника и доказательств. Фоновый запуск не решает
CAPTCHA: Chrome-плагин требует подтверждения непосредственно перед решением.
При `CODEX_OLX_FALLBACK_ENABLED=true` та же модель и Chrome-профиль используются
для OLX только после таймаута AGY/Playwright, подтверждённой ошибки доступа
или некорректного структурированного ответа AGY.
Агент открывает объявление и нажимает публичную кнопку показа телефона; номер
принимается только с доказательством на URL объявления. Разрешение Chrome для
`olx.kz` нужно проверить отдельно от разрешения для 2ГИС.
OLX fallback обрабатывает не более трёх карточек за один вызов Codex, исключает
уже принятые ID в следующем вызове и держит общий лимит времени на ветку.
Если модель пропустила `contact` в доказательстве номера, допускается один
повтор проверки карточки; без доказательства номер отклоняется. При сбое
позднего пакета ранее проверенные лиды сохраняются как неполный результат.
Модель fallback задаётся `CODEX_2GIS_MODEL` (по умолчанию `gpt-6-luna`), усилие
рассуждения — `CODEX_2GIS_REASONING_EFFORT` (по умолчанию `high`).
Для 2GIS агент получает признаки товара из техспецификации, выбирает типы
магазинов и ищет их в заданной территории. Он открывает карточки компаний,
фиксирует показанные номера как `phone` и сохраняет ссылку на карточку в
доказательстве. Номер становится `whatsapp` только при явной ссылке WhatsApp
или положительной проверке через локальный WAHA `check-exists`. Проверка перед
подготовкой сообщений записывается в `workflow_events`; отрицательный ответ
сохраняет телефонный лид. Отправка по-прежнему проходит approval gate.
Перед включением fallback проверьте, что Chrome-плагину разрешён доступ к
`2gis.kz` из серверного Codex CLI. Отказ браузерного разрешения останавливает
поиск до загрузки страницы; фоновой worker не может подтвердить запрос за
оператора. При таком отказе оставьте `CODEX_2GIS_FALLBACK_ENABLED=false`.
Инструкции для дочернего Codex лежат в
`skills/2gis-chrome-research/SKILL.md`; на сервере их нужно установить в
`~/.codex/skills/2gis-chrome-research/SKILL.md` перед включением fallback.
Для проверки разрешения в существующем профиле Chrome запустите на сервере
из обычного терминала `bash scripts/authorize_2gis_chrome.sh` и ответьте на
запрос браузера, если он появится. Для будущих фоновых запусков требуется
вариант `Always allow` для `https://2gis.kz`; разовое `Allow` действует только
в интерактивной сессии. Скрипт не меняет состояние заявки или БД.

Локальный WAHA зафиксирован на официальном Core/NOWEB `2026.9.1` и запускается через
`deploy/waha/compose.yaml`. API публикуется только на `127.0.0.1:13000`, состояние WhatsApp
хранится в named volume, а webhook доступен WAHA только через выделенный Docker bridge.
`deploy/install_waha_native.sh` сохранён как аварийный fallback без Docker. QR получается без
ввода Dashboard credentials:

```bash
.venv/bin/python -m procurement_bot.local_bootstrap qr --root /home/moses/tenders
```

Файл `var/waha-qr.png` является краткоживущим секретом сопряжения и имеет права `600`.
Если сессия WAHA временно переходит в `FAILED`, отдельный таймер раз в пять
минут проверяет локальный API и запрашивает restart не чаще раза в 30 минут.
Он не отправляет сообщений и не трогает состояния QR/входа. На новом хосте:

```bash
install -d -m 700 var/run
systemctl --user link deploy/systemd-user/procurement-waha-session-watch.service
systemctl --user link deploy/systemd-user/procurement-waha-session-watch.timer
systemctl --user enable --now procurement-waha-session-watch.timer
```

Research и WhatsApp send approval-кнопки разделены: разрешение на поиск не даёт разрешения
писать поставщику. Текущий WAHA-адаптер fail-closed; каждый исходящий payload согласуется
отдельно и повторно проверяется в момент доставки. Для crash-safe retry он требует WAHA
`2026.4.3+` с GOWS или NOWEB: перед
`sendText` вызывается `GET /api/{session}/new-message-id`, а полученный `id` сохраняется в
outbox. WEBJS не должен использоваться для production-отправки этого сервиса.

## Проверка

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

PostgreSQL integration test автоматически включается при наличии одноразовой БД:

```bash
TEST_POSTGRES_DSN='postgresql:///procurement_bot_test' .venv/bin/pytest -q -m postgres
```

Тестовая БД должна быть отдельной и доступной на создание таблиц/extensions. Никогда не
указывайте production DSN.

## Запросы к данным и бухгалтерия

AGY не получает SQL-консоль и не создаёт PostgreSQL-таблицы из текста пользователя. Новые
структуры появляются только через versioned migrations. В Telegram поддерживается небольшой
детерминированный язык read-only запросов. При нескольких активных заявках бот просит указать
название или город и не показывает пользователю внутренние UUID/хэши.

Финансовый результат является предварительным, пока нет тендерной выручки, цены хотя бы одной
покупки или подтверждённых логистических затрат. Внутренняя сумма тендера хранится отдельно и
никогда не входит в browser/search/WhatsApp payload. Общая поездка по умолчанию делится поровну
между явно выбранными строками; остаток до копейки распределяется детерминированно, так что
сумма allocations точно равна стоимости доставки.

## Границы текущего состояния

- live browser worker включён на текущем сервере и проверен на 2ГИС:
  AGY после `captcha` переключился на Codex Chrome, открыл карточку фирмы и
  сохранил контакт с URL-доказательством. Это один подтверждённый контакт,
  а не гарантия полноты поиска;
- разговорная география проходит отдельный browser lookup; результат хранится версионно,
  high-confidence вариант применяется детерминированно, иначе бот просит выбрать ориентир;
- initial outreach создаётся для точной позиции и выбранного контакта;
  обычный телефон остаётся лидом с неподтверждённым WhatsApp, пока связь
  не проверена. Новая позиция с тем же контактом ведётся последовательно;
  сообщения отправляются только после одобрения точного текста;
- HTTP ingress принимает только текст; media replies пока безопасно игнорируются, а сложные
  ответы без однозначных сигналов требуют проверки человеком;
- актуальное сравнение считает только цену товара на партию с pack/MOQ;
  доставка учитывается по внесённому факту, НДС не рассчитывается. Старый
  `costing.py` с landed-cost/VAT сохранён как неиспользуемый legacy-модуль.
  Есть фактический accounting ledger, но автоматический
  OCR/разбор строк чека и UI подтверждения нескольких позиций из одного чека ещё не завершены;
  recommendation set пока не персистится;
- нет OCR для фото/сканов и chunked intake документов свыше 49k символов;
- внешние outbox имеют at-least-once семантику: provider idempotency key снижает риск, но
  crash после API send и до DB commit всё ещё требует reconciliation.
- browser-result payload сохраняется и хешируется приложением, но сырые DOM/network snapshots
  конкретного MCP пока не архивируются как отдельные файлы.

Полная целевая схема и последующие этапы описаны в
[ARCHITECTURE_AGY_TENDER_PROCUREMENT_BOT.md](/home/moses/tenders/ARCHITECTURE_AGY_TENDER_PROCUREMENT_BOT.md).

## Existing procurement knowledge base

This repository stores the working instructions, Codex skill, references, and helper scripts for Kazakhstan tender/procurement work with Notion.

The product is not limited to Nazarbayev Intellectual Schools. NIS requests are a frequent and well-known customer pattern, but the workflow must support any organization: schools, colleges, universities, clinics, offices, public-sector bodies, private companies, contractors, and one-off commercial requests.

## Layout

- `skills/tender-procurement-notion/` - installable Codex skill.
- `skills/tender-procurement-notion/references/` - detailed workflow references loaded only when needed.
- `skills/tender-procurement-notion/scripts/` - small deterministic helpers.
- `src/tender_agent/` - dependency-free deterministic core and safe public HTML extractor.
- `tests/` - unit tests and local HTML fixtures.
- `docs/` - human-readable audit, pipeline, and roadmap.
- root legacy `*.md` files - original project instructions used as source material.

Key docs:

- `docs/tender-agent-master-implementation-plan.md` - active master plan; update it before changing the architecture, workflow, skill, Notion schema, or implementation.
- `docs/pipeline-v3-architecture.md` - architecture and failure boundaries of the durable procurement core.
- `docs/pipeline-v3-implementation-report.md` - implemented contracts, tests, commands and remaining adapter work.
- `docs/tender-agent-pipeline.md` - operational procurement pipeline.
- `docs/automation-roadmap.md` - next helper scripts and guardrails.
- `docs/github-repo-scout.md` - decision on external GitHub projects.
- `docs/product-and-messaging-architecture.md` - product boundary, WhatsApp/Telegram options, Ubuntu deployment model.
- `docs/telegram-whatsapp-implementation-plan.md` - phased implementation plan for the persistent Telegram/WhatsApp procurement agent.
- `docs/notion-maxim-structure.md` - actual Notion structure under `Максим` and required request/supplier relation workflow.
- `docs/offers-database-field-guide.md` - complete human guide to every `Предложения` field.
- `docs/notion-cleanup-1.6-report.md` - verified reset report for `Караганда_1` and supplier legacy.

## Deterministic Core

Run all local checks:

```bash
./scripts/run_checks.sh
```

Examples:

```bash
PYTHONPATH=src python3 -m tender_agent allocate-delivery \
  --total 5000 №8=1 №12=1 №15=1

PYTHONPATH=src python3 -m tender_agent extract-file \
  tests/fixtures/product_jsonld.html --source-url https://shop.example/item

PYTHONPATH=src python3 -m tender_agent source-plan \
  https://kaspi.kz/shop/p/test --html tests/fixtures/product_jsonld.html \
  --required product_name price confirmed_stock

PYTHONPATH=src python3 -m tender_agent compare-scenarios tests/fixtures/scenarios.json
PYTHONPATH=src python3 -m tender_agent trip-economics tests/fixtures/trip.json

PYTHONPATH=src python3 -m tender_agent snapshot-validate \
  tests/fixtures/karaganda_reconciliation.json
PYTHONPATH=src python3 -m tender_agent reconcile \
  tests/fixtures/karaganda_reconciliation.json
PYTHONPATH=src python3 -m tender_agent report \
  tests/fixtures/karaganda_reconciliation.json --format md

PYTHONPATH=src python3 -m tender_agent store-init var/tender-agent.sqlite
PYTHONPATH=src python3 -m tender_agent job-status var/tender-agent.sqlite

PYTHONPATH=src python3 -m tender_agent notion-plan \
  tests/fixtures/notion_current.json tests/fixtures/notion_desired.json
PYTHONPATH=src python3 -m tender_agent coverage tests/fixtures/coverage.json \
  --minimum-leads 2 --minimum-actionable 2
```

`extract-url` accepts only public HTTP(S) HTML pages, limits redirects, response size and timeout, and does not bypass login or anti-bot protection. A public stock signal is never converted into supplier-confirmed stock automatically.

Pipeline v3 stores source assertions as append-only facts, calculates line state on independent procurement/delivery/location/acceptance/replacement axes, and emits deterministic snapshots. Notion is a projection: `notion-plan` is always read-only, and runtime application must use the verified adapter contract from `tender_agent.notion_sync`. Five URLs are no longer treated as five actionable offers; `coverage` checks maturity, freshness, technical proof, confirmed price, contactability, local coverage and a reserve option.

## Install In Codex

Install or refresh the skill:

```bash
./scripts/install_skill.sh
```

The skill is copied to:

```text
~/.codex/skills/tender-procurement-notion
```

That makes it visible to Codex desktop and Codex CLI after the next skill discovery/restart.

## Validation

Validate the skill metadata:

```bash
./scripts/validate_skill.sh
```

The official `quick_validate.py` can also be used when `PyYAML` is installed.

Run a helper script smoke test:

```bash
python3 skills/tender-procurement-notion/scripts/check_candidates.py examples/candidates.csv --price-limit 2000 --required-qty 9
```

## Operating Principle

The agent may research, compare, draft, and update Notion. It must not send messages, reserve goods, place orders, or pay without explicit user approval in the same conversation.
