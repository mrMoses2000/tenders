# WhatsApp API и браузерная автоматизация для AGY

Дата проверки: 22 сентября 2026 года
Среда: Linux, AGY CLI 1.2.7, Node.js 22.23.1, Google Chrome
Исходный документ: `/home/moses/Downloads/AGY_CLI_BROWSER_AUTOMATION_RESEARCH.md`

## 1. Краткий вывод

### WhatsApp

Лучший практический вариант под заданные условия — **WAHA Core**:

- бесплатный и self-hosted;
- Apache-2.0;
- REST API, webhooks, Swagger и Dashboard;
- подключает существующий WhatsApp/WhatsApp Business как связанное устройство по QR;
- телефонное приложение продолжает работать;
- с версии `2026.6.1` прежние платные Plus-функции перенесены в бесплатный Core;
- с версии `2026.4.3` имеет собственный MCP endpoint;
- MCP-доступ можно ограничить отдельными правами `read`, `send`, `control`, `setting`, `app`, `delete`.

Но WAHA, Baileys, whatsapp-web.js и WPPConnect — **неофициальные клиенты WhatsApp Web**. Они бесплатны по лицензии, но не дают нулевого риска: возможны разлогинивание, несовместимость после обновления WhatsApp и блокировка номера. WhatsApp прямо запрещает неразрешённые автоматические и массовые сообщения. Для спама или холодной массовой рассылки эти решения применять нельзя.

Если важнее отсутствие риска блокировки, чем бесплатность, нужен официальный **WhatsApp Business Platform / Cloud API**. Он не подходит под все три требования одновременно:

- это бизнес-платформа, а не API обычного WhatsApp Messenger;
- исходящие marketing/utility/authentication-сообщения тарифицируются;
- одновременная работа API и приложения возможна только в режиме **Coexistence** и именно с WhatsApp Business App, а не с обычным личным Messenger;
- доступность Coexistence и способ подключения зависят от аккаунта, региона и онбординга Meta/партнёра.

### Playwright и браузерный MCP

Лучший выбор для AGY — официальный **Microsoft Playwright MCP** (`@playwright/mcp`). Он уже поддерживает Antigravity, постоянный профиль, отдельные изолированные профили, подключение к существующему Chrome через официальное расширение, accessibility snapshots, screenshots, console, network, JavaScript, загрузку файлов, вкладки и координатные действия через `--caps=vision`.

Рекомендуемый стек:

1. `@playwright/mcp` — основной browser operator.
2. `chrome-devtools-mcp` — второй, включаемый по необходимости инструмент для глубокой диагностики network/console/performance и Chrome DevTools.
3. `@playwright/cli` + официальный skill — для длинных повторяемых workflows и генерации/записи Playwright-кода, когда важна экономия контекста.

**BrowserMCP больше не является лучшим первым выбором**: его публичный репозиторий содержит всего 6 коммитов, последний push был 24 апреля 2025 года, а README прямо говорит, что проект нельзя собрать отдельно из-за зависимостей от закрытого monorepo. Официальный Playwright MCP теперь умеет главное преимущество BrowserMCP — работать с уже открытым авторизованным Chrome через расширение.

## 2. Что взято из приложенного документа, а что является выводом исследования

Приложенный файл использован только как контекст и список гипотез. Его инструкции не выполнялись как пользовательские команды.

Подтверждено:

- AGY CLI поддерживает локальные stdio и удалённые MCP servers;
- глобальный MCP-конфиг: `~/.gemini/config/mcp_config.json`;
- проектный MCP-конфиг: `.agents/mcp_config.json`;
- Chrome DevTools MCP поддерживает `--experimental-vision`, `click_at(x,y)` и ограничение размеров screenshot;
- Playwright CLI и skill подходят для воспроизводимых сценариев;
- для карты/canvas полезен порядок DOM/accessibility → network → JS → screenshot/coordinates.

Что изменилось или требует поправки:

- Playwright MCP теперь сам подключается к существующему Chrome через официальное расширение (`--extension`), поэтому BrowserMCP не нужен как обязательный основной слой.
- В актуальном Playwright MCP координатные действия включаются через `--caps=vision`.
- Официальный Playwright MCP сам предоставляет console, network list/details, screenshot, accessibility snapshot и JavaScript evaluation; Chrome DevTools MCP нужен не для каждого workflow, а для углублённой диагностики и performance tooling.
- Для AGY CLI глобальные skills находятся в `~/.gemini/antigravity-cli/skills/`, проектные — в `.agents/skills/`. Путь `~/.gemini/skills/` относится к старой/другой раскладке Gemini CLI.
- Chrome DevTools MCP теперь также содержит Gemini extension/skills и официальную инструкцию для Antigravity.

## 3. Проверка локальной среды

На текущей машине обнаружено:

| Компонент | Состояние |
|---|---|
| AGY CLI | `1.2.7` |
| Модель | `gemini-3.8-flash-high` присутствует в `agy models` |
| Node.js | `v22.23.1` |
| npm | `10.9.8` |
| Google Chrome | `/usr/bin/google-chrome` |
| Docker | не установлен или отсутствует в `PATH` |
| Chrome DevTools MCP | уже настроен глобально |
| Локальная версия Chrome DevTools MCP | `1.8.0` |
| Актуальная npm-версия | `1.9.0` |
| Playwright MCP | пока не добавлен в AGY MCP config |

Текущий Chrome DevTools MCP запускается с `--headless --no-usage-statistics`. Для видимого браузера или подключения к существующей сессии конфигурацию нужно менять отдельно.

## 4. Два класса WhatsApp-решений

| Критерий | Официальный Cloud API | Неофициальный WhatsApp Web client |
|---|---|---|
| Обычный личный WhatsApp Messenger | Нет | Да |
| Продолжать пользоваться телефоном | Только Business App + Coexistence | Да, автоматизация видна как linked device |
| Полностью бесплатные сообщения | Нет | Нет платы Meta за API, но есть серверные расходы |
| Официально разрешённая автоматизация | Да | Нет |
| Риск блокировки номера | Минимальный при соблюдении правил | Реальный, невозможно свести к нулю |
| Шаблоны/24-часовые окна | Да | Нет API-ограничений Cloud API; действуют обычные правила приложения |
| Быстрый запуск с личным номером | Нет | Да, QR/pairing code |
| Стабильность протокола | Высокая | Зависит от изменений WhatsApp Web |

На 22 сентября 2026 года текущая публичная страница Meta указывает, что service messages внутри 24-часового окна бесплатны, но остальные категории тарифицируются. Поэтому Cloud API нельзя называть полностью бесплатным. Перед production-запуском тарифы следует перепроверять повторно: они меняются по датам, категории и рынку получателя.

## 5. GitHub-проекты WhatsApp

Метрики ниже сняты 22 сентября 2026 года и со временем изменятся.

| Проект | Stars | Последняя активность/release | Лицензия | Класс | Вердикт |
|---|---:|---|---|---|---|
| [devlikeapro/waha](https://github.com/devlikeapro/waha) | 7.4k | release `2026.9.1`, 22.09.2026 | Apache-2.0 | REST + MCP facade | **Использовать напрямую** |
| [WhiskeySockets/Baileys](https://github.com/WhiskeySockets/Baileys) | 11.1k | push 20.09.2026, `v7.0.0-rc14` | MIT | прямой WebSocket | **База для своей интеграции** |
| [wwebjs/whatsapp-web.js](https://github.com/wwebjs/whatsapp-web.js) | 22.6k | push 20.09.2026, `v1.34.7` | Apache-2.0 | Puppeteer/real browser | **Использовать как library** |
| [wppconnect-team/wppconnect-server](https://github.com/wppconnect-team/wppconnect-server) | 1.0k | `v2.10.27`, 17.09.2026 | Apache-2.0 | REST + browser | **Хороший резервный вариант** |
| [tulir/whatsmeow](https://github.com/tulir/whatsmeow) | 7.4k | push 21.09.2026 | MPL-2.0 | Go/WebSocket | **База для Go-сервиса** |
| [evolution-foundation/evolution-api](https://github.com/evolution-foundation/evolution-api) | 9.7k | push 14.07.2026; release от 2025 | Apache-2.0 + дополнительные условия | тяжёлая integration platform | **Только при нужде в CRM/queues** |
| [open-wa/wa-automate-nodejs](https://github.com/open-wa/wa-automate-nodejs) | 3.7k | v5 alpha; stable v4 `4.76.0` от 2025 | H-DNH custom | API/plugins/MCP | **Reference only** |

### 5.1. WAHA — основная рекомендация

WAHA — готовый слой над несколькими движками:

- `WEBJS`: реальный Chromium/Chrome через Puppeteer, наиболее предсказуемый старт;
- `WPP`: браузерный WPPConnect;
- `NOWEB`: прямой WebSocket через форк Baileys;
- `GOWS`: Go-сервис поверх форка whatsmeow.

Сильные стороны:

- единый REST API независимо от движка;
- QR login, сессии, сообщения, media, contacts, groups, labels, presence;
- webhooks и event stream;
- Swagger и dashboard;
- локальное/PostgreSQL/S3 storage;
- встроенный MCP server по `http://localhost:3000/mcp`;
- отдельный scoped key на MCP app;
- всё прежнее WAHA Plus с `2026.6.1` входит в Core бесплатно;
- очень высокая текущая активность и релиз в день исследования.

Слабые стороны:

- Docker — рекомендуемый путь, а на текущей машине Docker отсутствует;
- browser engine потребляет больше RAM/CPU;
- NOWEB/GOWS легче, но повторяют неофициальный протокол и могут ломаться после изменений WhatsApp;
- API payloads разных engines не полностью идентичны;
- сам проект не превращает неофициальную автоматизацию в разрешённую Meta.

Рекомендация по engine:

1. Первый пилот — `WEBJS`: меньше сюрпризов, реальный WhatsApp Web.
2. После функционального теста — сравнить `GOWS`: существенно ниже потребление ресурсов.
3. Не переключать engine в production без contract tests: документация предупреждает, что payloads могут отличаться.

### 5.2. Baileys — лучший низкоуровневый Node/TypeScript вариант

Baileys говорит с WhatsApp Web напрямую по WebSocket/Noise/protobuf и не запускает Chromium.

Плюсы:

- низкое потребление памяти;
- QR и pairing code;
- send/receive, media, groups, receipts, presence и события;
- хороший выбор для собственного backend;
- MIT и активная поддержка.

Минусы:

- это library, а не готовый безопасный REST service;
- auth state, reconnect, очереди, rate limit, webhooks, idempotency и observability придётся проектировать;
- текущая ветка 7.x содержит breaking changes и release candidate;
- неверное хранение `auth state` компрометирует аккаунт.

Вердикт: брать как основу, если нужен собственный TypeScript-сервис и команда готова владеть протокольным слоем. Для быстрого API лучше WAHA.

### 5.3. whatsapp-web.js — максимальная близость к реальному Web client

Проект запускает WhatsApp Web через Puppeteer и экспортирует его возможности в Node.js.

Плюсы:

- крупнейшее сообщество среди рассмотренных библиотек;
- богатая функциональность;
- реальный браузер иногда лучше переносит изменения протокола;
- Apache-2.0.

Минусы:

- Chromium требует заметно больше RAM/CPU;
- DOM/internal-function changes WhatsApp Web всё равно ломают интеграцию;
- проект прямо предупреждает, что блокировка не исключена и боты/неофициальные клиенты WhatsApp не разрешены.

Вердикт: хороший library-level выбор, но WAHA уже предоставляет готовый API поверх форка WEBJS.

### 5.4. WPPConnect Server — сильная готовая альтернатива WAHA

Плюсы:

- REST, Swagger/Postman, tokens, webhooks, multiple sessions;
- text/media/docs, contacts, groups, products;
- готовый Docker image с Chromium;
- активный релизный цикл.

Минусы:

- browser-heavy;
- нет столь удачного встроенного scoped MCP слоя, как в WAHA;
- для AGY понадобится вызывать REST через отдельный MCP wrapper или shell/tool.

Вердикт: использовать, если WAHA несовместим с нужной функцией или WPP engine показывает лучшую стабильность на конкретном аккаунте.

### 5.5. whatsmeow — сильный Go building block

Плюсы:

- direct WebSocket, без браузера;
- MPL-2.0;
- send/receive text/media, groups, receipts, app state, presence;
- низкое потребление ресурсов.

Минусы:

- library, не REST/MCP product;
- не реализованы calls и broadcast-list sending;
- потребует собственного сервисного слоя.

Вердикт: отличный fork base для Go; косвенно уже доступен через WAHA GOWS.

### 5.6. Evolution API — мощно, но тяжелее и с лицензионной оговоркой

Evolution объединяет Baileys и официальный Cloud API, а также Chatwoot, Typebot, Dify, OpenAI, RabbitMQ, Kafka, SQS, Socket.io, S3/MinIO.

Плюсы:

- production-oriented integration hub;
- event-driven architecture;
- PostgreSQL/MySQL, queues, media storage, webhooks.

Минусы:

- для одного аккаунта это избыточный стек;
- требует БД и желательно Redis;
- лицензия названа Apache-2.0, но добавляет обязательное уведомление об использовании и ограничения на удаление branding frontend; при невыполнении требуется commercial license;
- latest GitHub release заметно старее текущего кода.

Вердикт: reference/fork only, если действительно нужны CRM/queues/multi-provider. Для простого бесплатного API — WAHA проще и чище.

### 5.7. open-wa — интересен MCP, но не лучший production base

Проект v5 имеет HTTP API, runtime, plugins, webhooks и встроенный MCP, но сам README помечает v5 как alpha и рекомендует держать production на v4.76.0. Лицензия H-DNH содержит дополнительные этические ограничения и не является обычной permissive open-source лицензией.

Вердикт: изучать идеи MCP/plugin architecture, но не начинать новый production именно на нём.

## 6. Ответ на вопрос «смогу ли я продолжать заходить в WhatsApp?»

Для WAHA/Baileys/whatsapp-web.js/WPPConnect — **да**:

- телефон остаётся primary device;
- сервис регистрируется как linked device;
- вы продолжаете читать и писать с телефона;
- можно также использовать другие разрешённые linked devices, пока не исчерпан лимит WhatsApp;
- в приложении `Linked devices` можно увидеть и отключить серверную сессию.

Практические оговорки:

- разлогинивание linked device инвалидирует сохранённую сессию API;
- повторный QR login может понадобиться после обновлений или защитных событий;
- удаление server auth volume означает новый login;
- нельзя одновременно запускать несколько процессов с одним и тем же auth store;
- не следует тестировать это первым делом на незаменимом личном номере.

Для официального Cloud API одновременное использование возможно не с обычным Messenger, а с WhatsApp Business App через Coexistence.

## 7. Рекомендуемая архитектура WhatsApp → AGY

```text
WhatsApp phone app
        │ QR / linked device
        ▼
WAHA Core (WEBJS first, GOWS after validation)
        ├── REST API        → ваше приложение
        ├── Webhooks        → backend / queue / audit
        └── Scoped MCP      → AGY CLI
                                 │
                                 └── Gemini 3.8 Flash High
```

### 7.1. Минимальный безопасный WAHA pilot

Docker на машине пока не установлен. После установки Docker запускать не `latest`, а закреплённый проверенный tag, например актуальный на дату исследования `2026.9.1`.

```bash
docker run -d \
  --name waha \
  --restart unless-stopped \
  -p 127.0.0.1:3000:3000 \
  -e WAHA_APPS_ENABLED=True \
  -e WAHA_API_KEY=REPLACE_WITH_LONG_RANDOM_SECRET \
  -e WHATSAPP_DEFAULT_ENGINE=WEBJS \
  -v waha_sessions:/app/.sessions \
  devlikeapro/waha:latest-2026.9.1
```

Почему `127.0.0.1`: Dashboard, REST и MCP не должны быть опубликованы в интернет напрямую.

Дальше:

1. Открыть локальный Dashboard/Swagger.
2. Создать session `default`.
3. Отсканировать QR в WhatsApp → Linked devices.
4. Проверить отправку одному собственному тестовому контакту.
5. Создать отдельный MCP app для этой session.
6. На первом этапе дать MCP только `send=true`; `read=true` включить лишь когда это необходимо.

### 7.2. Scoped MCP app

```http
POST http://127.0.0.1:3000/api/apps
X-Api-Key: <WAHA_ADMIN_KEY>
Content-Type: application/json

{
  "enabled": true,
  "id": "app_mcp_default",
  "session": "default",
  "app": "mcp",
  "config": {
    "actions": {
      "read": false,
      "send": true,
      "control": false,
      "setting": false,
      "app": false,
      "delete": false
    }
  }
}
```

Ответ вернёт отдельный scoped key. Не использовать административный `WAHA_API_KEY` в AGY.

### 7.3. Подключение WAHA MCP к AGY

В `~/.gemini/config/mcp_config.json` или `.agents/mcp_config.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "serverUrl": "http://127.0.0.1:3000/mcp",
      "headers": {
        "X-Api-Key": "REPLACE_WITH_SCOPED_MCP_KEY"
      }
    }
  }
}
```

AGY использует именно `serverUrl`, не устаревшие `url`/`httpUrl`. Файл с ключом должен иметь права `0600`. После изменения открыть `/mcp`, reload и проверить список tools.

### 7.4. Ограничения, которых не хватает одному scope

Даже `send-only` разрешение слишком широкое, если агент способен писать любому JID. Для production желательно поставить перед WAHA маленький policy gateway:

- allowlist номеров/групп;
- максимум сообщений в минуту/час;
- запрет новых получателей без human approval;
- лимит длины и размера media;
- idempotency key;
- audit log: кто, когда, кому, какой workflow инициировал отправку;
- dry-run mode;
- kill switch;
- запрет массовой отправки.

## 8. Browser automation: проекты и вердикты

| Проект | Stars | Активность | Лицензия | Вердикт |
|---|---:|---|---|---|
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | 37.5k | `v0.0.82`, 18.09.2026 | Apache-2.0 | **Использовать напрямую** |
| [microsoft/playwright-cli](https://github.com/microsoft/playwright-cli) | 13.5k | `v0.1.21`, 18.09.2026 | Apache-2.0 | **Добавить как skill/CLI** |
| [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | 52.4k | `v1.9.0`, 08.09.2026 | Apache-2.0 | **Использовать как diagnostic companion** |
| [browserbase/stagehand](https://github.com/browserbase/stagehand) | 24.8k | `3.7.3`, 28.08.2026 | MIT | **Reference/fork для self-healing** |
| [BrowserMCP/mcp](https://github.com/BrowserMCP/mcp) | 7.1k | последний push 24.04.2025 | Apache-2.0 | **Не брать основным** |
| [executeautomation/mcp-playwright](https://github.com/executeautomation/mcp-playwright) | 5.7k | последний push 13.12.2025 | MIT | **Отклонить в пользу Microsoft** |
| [browserbase/mcp-server-browserbase](https://github.com/browserbase/mcp-server-browserbase) | 3.4k | archived | Apache-2.0 | **Отклонить для нового проекта** |

### 8.1. Microsoft Playwright MCP — основной выбор

Почему это лучший вариант:

- официальный Microsoft project;
- явная инструкция для Antigravity;
- deterministic interaction по accessibility snapshot;
- работает без обязательной vision-модели для обычных DOM controls;
- browser extension подключает существующие Chrome tabs и cookies;
- постоянный профиль по умолчанию;
- isolated mode + storage state для тестов;
- console и network request inspection;
- screenshot и full-page screenshot;
- JavaScript evaluation;
- tab management, uploads, dialogs, drag/drop;
- `--caps=vision` добавляет coordinate click/move/drag/wheel;
- `--caps=testing` добавляет проверки и locator generation;
- code generation/recording позволяет превращать исследовательский проход в стабильный Playwright test.

Риски:

- MCP schema и accessibility trees расходуют больше контекста, чем CLI;
- `browser_run_code_unsafe` эквивалентен выполнению произвольного кода на MCP host — его нельзя автоматически разрешать без необходимости;
- extension mode даёт агенту доступ к данным реального профиля и только тем tabs/groups, которые подключены; это всё равно чувствительный доступ;
- persistent profile нельзя одновременно открыть двумя browser instances.

### 8.2. Playwright CLI + skill

Microsoft сам рекомендует CLI+SKILLS для coding agents, когда важна экономия токенов, а MCP — для persistent exploratory loops.

Установка:

```bash
npm install -g @playwright/cli@0.1.21
playwright-cli install --skills
```

Для AGY проверить, что skill попал в один из актуальных каталогов:

- глобально: `~/.gemini/antigravity-cli/skills/`;
- в проекте: `.agents/skills/`.

Роль CLI:

- записать стабильный workflow после разведки через MCP;
- генерировать Playwright code;
- выполнять повторяемые сценарии с меньшим расходом контекста;
- сохранять screenshots/traces/tests как артефакты.

### 8.3. Chrome DevTools MCP — дополнение, не второй основной browser operator

Сильные стороны:

- официальный проект ChromeDevTools;
- console с source maps;
- network request/response;
- performance traces и Lighthouse/CrUX insights;
- screenshot и DOM interaction;
- подключение к существующему Chrome/CDP/Android;
- экспериментальный `click_at(x,y)` через `--experimental-vision`;
- официальный client config для Antigravity.

На этой машине он уже установлен и настроен, но локальная версия `1.8.0` отстаёт от npm `1.9.0`.

Для основной навигации не держать Playwright MCP и Chrome DevTools MCP одинаково приоритетными. В prompt явно указывать:

- Playwright — navigation/forms/extraction;
- Chrome DevTools — только network/console/performance/canvas fallback.

### 8.4. Stagehand

Stagehand добавляет AI-команды `act`, `observe`, `extract` и schema-validated extraction; может работать локально и с Playwright Page. Это интересно для self-healing workflows, но:

- нужен дополнительный model call/API key;
- появляется ещё один недетерминированный слой;
- cloud Browserbase не удовлетворяет строгому требованию бесплатности;
- старый Browserbase MCP server архивирован.

Вердикт: использовать как reference или второй этап, если обычные accessibility locators недостаточно устойчивы.

### 8.5. Почему не BrowserMCP

Ранее его главное преимущество состояло в работе с настоящим авторизованным Chrome. Теперь это умеет официальный Playwright Extension.

Дополнительные причины:

- публичный repo почти не обновляется;
- всего 6 коммитов;
- README признаёт невозможность standalone build из опубликованного repo;
- сервер адаптирован из Playwright MCP, то есть upstream теперь предпочтительнее.

BrowserMCP можно оставить как резерв, если конкретно его extension лучше проходит отдельный сайт, но строить новый основной стек на нём не стоит.

## 9. Рекомендуемый Playwright MCP config для AGY

### Вариант A — отдельный постоянный browser profile

Самый безопасный старт: отдельный видимый Chrome profile для агента.

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": [
        "-y",
        "@playwright/mcp@0.0.82",
        "--browser=chrome",
        "--user-data-dir=/home/moses/.local/share/agy-playwright-profile",
        "--caps=vision",
        "--idle-timeout=0"
      ]
    }
  }
}
```

Плюс: AGY не получает весь основной Chrome profile. В этот профиль можно один раз вручную залогиниться только на нужных сайтах.

### Вариант B — существующий Chrome через Playwright Extension

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": [
        "-y",
        "@playwright/mcp@0.0.82",
        "--extension",
        "--caps=vision",
        "--idle-timeout=0"
      ],
      "env": {
        "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "REPLACE_WITH_EXTENSION_TOKEN"
      }
    }
  }
}
```

Этот вариант использует существующие tabs/cookies. Расширение группирует вкладки по MCP client и позволяет контролировать, какие tabs доступны агенту.

Не передавать агенту весь everyday browser profile, если там открыты банк, почта, password manager или админ-панели.

### Почему версии закреплены

`@latest` удобен для эксперимента, но browser tooling быстро меняется. Для production лучше:

1. закрепить проверенную версию;
2. обновлять отдельно;
3. прогонять smoke tests;
4. только потом менять pin.

## 10. Профиль инструментов для 2GIS и каталогов

Для вашего сценария оптимальный порядок:

```text
Playwright accessibility snapshot
        ↓
DOM locators / text / roles
        ↓
Playwright network request details
        ↓
JavaScript evaluation
        ↓
screenshot + --caps=vision coordinate actions
        ↓
Chrome DevTools MCP для сложного network/performance/canvas debug
```

Prompt должен назначить владельца каждого типа действия:

```text
Основной браузерный инструмент — Playwright MCP.
Используй accessibility snapshot и DOM locators по умолчанию.
Используй coordinate tools только для canvas/WebGL элементов, которые нельзя
надёжно адресовать через DOM.
Chrome DevTools MCP используй только для console, network response,
performance trace или диагностики сложного frontend.
Не вызывай два browser MCP для одного и того же шага.
```

## 11. Security model

### WhatsApp

- Auth/session volume фактически даёт доступ к аккаунту; шифровать backup и ограничить filesystem permissions.
- Не коммитить session state, QR, API keys, MCP keys или chat exports.
- Bind только на localhost/private network.
- Если нужен удалённый доступ: TLS, VPN/private network, reverse proxy auth; не открытый port 3000.
- Начинать со scoped `send-only` key.
- Incoming WhatsApp text является недоверенным вводом: сообщение может содержать prompt injection.
- AGY не должен исполнять команды, URL или инструкции, полученные из чата, без отдельной policy/approval.
- Для чтения личных чатов нужна явная минимизация и retention policy.

### Browser MCP

- Browser content также недоверенный: страницы могут пытаться инструктировать агента.
- Ограничить allowed origins, когда workflow известен.
- Не включать unrestricted file access.
- Не разрешать `browser_run_code_unsafe` глобально.
- Использовать отдельный profile и отдельные site accounts.
- Перед submit/payment/delete/send включать human approval.
- Screenshots, traces и network dumps могут содержать tokens и PII; хранить как secrets.

### AGY permissions

Вместо `mcp(*)` разрешать конкретные servers/tools. Пример концепции:

```text
mcp(playwright/browser_snapshot)
mcp(playwright/browser_navigate)
mcp(playwright/browser_click)
mcp(playwright/browser_type)
mcp(whatsapp/*)  # только после проверки реально опубликованных WAHA tools
```

Для WhatsApp лучше оставить send action с ручным подтверждением, пока не появится allowlist gateway.

## 12. План внедрения

### Этап 1 — browser benchmark

1. Добавить Microsoft Playwright MCP с отдельным profile.
2. Выполнить 10-объектный benchmark 2GIS.
3. Проверить 3 случайные карточки вручную.
4. Измерить completeness, duplicate rate, wrong-card rate.
5. Проверить restart/resume.
6. Только затем включить `--caps=vision` для canvas fallback.

### Этап 2 — WhatsApp sandbox

1. Установить Docker.
2. Запустить pinned WAHA на localhost.
3. Использовать отдельный тестовый номер, не основной личный.
4. Проверить send/receive, reconnect, restart, logout/re-pair.
5. Создать send-only MCP app.
6. Подключить к AGY.
7. Проверить, что агент не может читать, удалять или менять настройки.

### Этап 3 — policy gateway

1. Allowlist 1–3 тестовых номеров.
2. Rate limit.
3. Human approval для первого сообщения новому контакту.
4. Audit log и idempotency.
5. Explicit opt-in/consent record.
6. Запрет массовых/холодных сообщений.

### Этап 4 — production decision

- Если блокировка номера недопустима — перейти на официальный Cloud API и выделенный Business number.
- Если сохранение телефона/личного аккаунта важнее и риск принят — WAHA, но только с отдельным рабочим номером и малым объёмом.
- Если нужен кастомный lightweight backend — постепенно заменить WAHA facade собственным сервисом на Baileys/whatsmeow, не раньше появления реальной причины.

## 13. Окончательный выбор

### Для WhatsApp

**Использовать напрямую:** WAHA Core, сначала WEBJS, затем протестировать GOWS.
**Fork base:** Baileys для TypeScript или whatsmeow для Go.
**Резерв:** WPPConnect Server.
**Reference only:** Evolution API и open-wa.
**Не обещать:** нулевой ban risk у любого неофициального решения.

### Для AGY browser automation

**Использовать напрямую:** Microsoft Playwright MCP.
**Добавить как workflow skill:** Microsoft Playwright CLI.
**Оставить вторым диагностическим сервером:** Chrome DevTools MCP.
**Reference only:** Stagehand.
**Не выбирать основой:** BrowserMCP, executeautomation/mcp-playwright, archived Browserbase MCP.

## 14. Основные источники

### WhatsApp и Meta

- WhatsApp Terms of Service: https://www.whatsapp.com/legal/terms-of-service
- WhatsApp Business Platform pricing: https://whatsappbusiness.com/products/platform-pricing/
- WhatsApp Cloud API collection by Meta: https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api
- WhatsApp Business app overview: https://faq.whatsapp.com/641572844337957

### WhatsApp open source

- WAHA: https://github.com/devlikeapro/waha
- WAHA MCP: https://waha.devlike.pro/docs/apps/mcp/
- WAHA engines: https://waha.devlike.pro/docs/engines/
- WAHA Core/Plus status: https://waha.devlike.pro/docs/how-to/waha-plus/
- Baileys: https://github.com/WhiskeySockets/Baileys
- whatsapp-web.js: https://github.com/wwebjs/whatsapp-web.js
- WPPConnect Server: https://github.com/wppconnect-team/wppconnect-server
- whatsmeow: https://github.com/tulir/whatsmeow
- Evolution API: https://github.com/evolution-foundation/evolution-api
- open-wa: https://github.com/open-wa/wa-automate-nodejs

### Browser automation / AGY

- Microsoft Playwright MCP: https://github.com/microsoft/playwright-mcp
- Playwright Chrome extension: https://github.com/microsoft/playwright/tree/main/packages/extension
- Microsoft Playwright CLI: https://github.com/microsoft/playwright-cli
- Chrome DevTools MCP: https://github.com/ChromeDevTools/chrome-devtools-mcp
- Chrome DevTools MCP client configurations: https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/client-configurations.md
- BrowserMCP: https://github.com/BrowserMCP/mcp
- Stagehand: https://github.com/browserbase/stagehand
- AGY MCP docs: https://antigravity.google/docs/mcp
- AGY plugins: https://antigravity.google/docs/plugins?tab=cli
- AGY migration/config paths: https://antigravity.google/docs/cli/gcli-migration/
