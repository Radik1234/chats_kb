# База знаний 1С на OpenSearch

Локальный стек: **OpenSearch 3.8** + **OpenSearch Dashboards** + сервис **ingest**,
который забирает JSON-выгрузки Telegram из `data/telegram/inbox` и кладёт
сообщения в месячные индексы `{chat_id}_{slug}_{YYYY-MM}`. Календарный день
сообщения хранится в поле `message_date`, поэтому выборка «за день» — это фильтр
по `message_date`, а не отдельный индекс.

Официальный OpenSearch MCP подключается профилем `mcp`, когда Docker может
ходить в PyPI. Эмбеддинги в этой итерации не входят.

Опциональный сервис **importer** (профиль `backfill`) по расписанию в один
поток дозагружает новые сообщения известных чатов из Telegram в `inbox` —
см. [фоновую дозагрузку](docs/backfill-worker.md).

## Быстрый старт (Windows + Docker Desktop)

```powershell
Set-Location F:\AI\Projects\onec_kb
Copy-Item .env.example .env   # если ещё нет .env
.\scripts\bootstrap.ps1
New-Item -ItemType Directory -Force data\telegram\inbox\1393071168_mssqlplus1c | Out-Null
Copy-Item chats_history\1393071168_mssqlplus1c\result.json data\telegram\inbox\1393071168_mssqlplus1c\
# дождаться файла в data\telegram\archive\1393071168_mssqlplus1c\
.\scripts\smoke-test.ps1
```

- Dashboards: http://127.0.0.1:5601 (логин `admin`, пароль из `.env`)
- OpenSearch REST: https://127.0.0.1:9200
- MCP Streamable HTTP: http://127.0.0.1:9900/mcp (профиль `mcp`)

Порты проброшены только на `127.0.0.1`.

## Документация

- [Развёртывание](docs/deployment-windows.md)
- [Импорт любого чата](docs/import-telegram.md)
- [Фоновая дозагрузка (importer-worker)](docs/backfill-worker.md)
- [MCP для агентов](docs/mcp-setup.md)
