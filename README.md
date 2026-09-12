# Локальная база знаний 1С

Локальный стек Beever Atlas v0.2.0 для импорта Telegram Desktop JSON, поиска
сырых сообщений, wiki/graph и MCP. LLM и embeddings выполняются локально через
два llama.cpp-сервера; базы данных наружу не публикуются.

## Быстрый старт (Windows 10 + Docker Desktop)

```powershell
Set-Location F:\AI\Projects\onec_kb
.\scripts\bootstrap.ps1
.\scripts\download-models.ps1 -Model All
docker compose up -d
.\scripts\smoke-test.ps1
```

Откройте `https://atlas.localhost`. Сертификат выпущен локальным CA Caddy;
порядок доверия сертификату описан в документации развёртывания. `.env`,
модели, данные, backups и Telegram exports исключены из Git.

## Основные операции

```powershell
# Состояние
docker compose ps
.\scripts\smoke-test.ps1

# Согласованный backup (на время снимка сервисы данных останавливаются)
.\scripts\backup.ps1

# Восстановление с проверкой SHA-256; операция заменяет целевые data volumes
.\scripts\restore.ps1 -BackupPath .\backups\onec-kb-YYYYMMDD-HHMMSS -Confirm:$false

# Остановка без удаления данных
docker compose stop
```

Не запускайте `docker compose down -v` для рабочего project: ключ `-v` удаляет
данные. Restore требует backup той же закреплённой версии стека.

## Документация

- [Развёртывание Windows](docs/deployment-windows.md)
- [Импорт Telegram](docs/import-telegram.md)
- [Подключение MCP](docs/mcp-setup.md)
- [Backup и restore](docs/backup-restore.md)
- [Итоговая приёмка](docs/acceptance-checklist.md)

Изменения внутри игнорируемого `vendor/beever-atlas` воспроизводятся через
`patches/beever-atlas-v0.2.0-onec-kb.patch` и проверяемый SHA-256 manifest.
