# База знаний 1С на OpenSearch

Локальный стек: **OpenSearch 3.8** + **OpenSearch Dashboards**. История Telegram-чатов
импортируется в отдельные индексы вида `{chat_id}_{slug}_{YYYY-MM-DD}`.
Официальный OpenSearch MCP подключается профилем `mcp`, когда Docker может
ходить в PyPI.

Эмбеддинги и кастомные skills в этой итерации не входят.

## Быстрый старт (Windows + Docker Desktop)

```powershell
Set-Location F:\AI\Projects\onec_kb
Copy-Item .env.example .env   # если ещё нет .env
.\scripts\bootstrap.ps1
.\scripts\import-chat.ps1 -Path .\chats_history\1393071168_mssqlplus1c\result.json -SnapshotDate 2026-09-17
.\scripts\smoke-test.ps1
```

- Dashboards: http://127.0.0.1:5601 (логин `admin`, пароль из `.env`)
- OpenSearch REST: https://127.0.0.1:9200
- MCP Streamable HTTP: http://127.0.0.1:9900/mcp (профиль `mcp`, когда доступен PyPI)

Порты проброшены только на `127.0.0.1`.

## Документация

- [Развёртывание](docs/deployment-windows.md)
- [Импорт любого чата](docs/import-telegram.md)
- [MCP для агентов](docs/mcp-setup.md)
