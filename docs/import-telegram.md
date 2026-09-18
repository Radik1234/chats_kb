# Импорт истории Telegram

Импортёр читает JSON-экспорт **Telegram Desktop** и создаёт индекс OpenSearch
на один снимок одного чата. Формат имени:

```text
{telegram_chat_id}_{slug}_{YYYY-MM-DD}
```

Пример: `1393071168_mssqlplus1c_2025-07-18`

- `telegram_chat_id` — поле `id` из `result.json` (не из имени файла).
- `slug` — короткое имя чата: из папки `{id}_{slug}`, либо `-Slug`, либо
  транслитерация `name`.
- `YYYY-MM-DD` — **дата снимка, которую вы задаёте**. Это идентификатор
  версии выгрузки (дата экспорта, контрольная дата, дата импорта), а не
  автоматически вычисленный диапазон сообщений. Диапазон дат сообщений
  пишется в каталог `kb_catalog`.

Повторный импорт в тот же индекс идемпотентен: документ
`{chat_id}_{message_id}` перезаписывается.

## 1. Экспорт из Telegram Desktop

1. Откройте нужный групповой чат.
2. Меню → **Export chat history**.
3. Формат: **JSON**. Медиа можно не включать (индексируется только текст).
4. Не переименовывайте поля `id`, `name`, `type`, `messages`, `text`,
   `from_id`, `reply_to_message_id`.
5. Положите `result.json` в каталог:

```text
chats_history/{telegram_chat_id}_{slug}/result.json
```

`slug` — латиница и цифры, без пробелов, например `mssqlplus1c`,
`buh_erp`, `zup`. Так индекс будет читаемым.

Файл может лежать в любом другом месте: скрипт монтирует родительский
каталог в контейнер импорта.

## 2. Что попадает в индекс

| Берём | Пропускаем |
| --- | --- |
| `type=message` с непустым текстом | service (инвайты, кики, смена названия) |
| текст-строка и составной `text[]` | пустые сообщения (стикеры/фото без подписи) |
| `reply_to_message_id`, автор, время | вложения как файлы (в выгрузке их нет) |

Производные поля: `has_links`, `has_logs`, `error_codes`, `is_bot`
(`via_bot` и известные боты).

## 3. Загрузка любого чата

Стек должен быть запущен (`.\scripts\bootstrap.ps1`).

```powershell
.\scripts\import-chat.ps1 `
  -Path .\chats_history\1393071168_mssqlplus1c\result.json `
  -SnapshotDate 2025-07-18
```

Другой чат:

```powershell
.\scripts\import-chat.ps1 `
  -Path .\chats_history\123456789_buh_erp\result.json `
  -SnapshotDate 2026-09-01
```

Явный slug, если папка не в формате `{id}_{slug}`:

```powershell
.\scripts\import-chat.ps1 `
  -Path C:\exports\result.json `
  -SnapshotDate 2026-09-17 `
  -Slug mssqlplus1c
```

Полное имя индекса вручную:

```powershell
.\scripts\import-chat.ps1 `
  -Path .\chats_history\1393071168_mssqlplus1c\result.json `
  -SnapshotDate 2025-07-18 `
  -IndexName 1393071168_mssqlplus1c_2025-07-18
```

Пересоздать индекс с тем же именем (удалит предыдущий снимок):

```powershell
.\scripts\import-chat.ps1 `
  -Path .\chats_history\1393071168_mssqlplus1c\result.json `
  -SnapshotDate 2025-07-18 `
  -Recreate
```

Проверка без записи в OpenSearch:

```powershell
.\scripts\import-chat.ps1 `
  -Path .\chats_history\1393071168_mssqlplus1c\result.json `
  -SnapshotDate 2025-07-18 `
  -DryRun
```

Тот же вызов напрямую через Python (так работает импорт на этой машине):

```powershell
python .\services\import\import_telegram.py `
  --file .\chats_history\1393071168_mssqlplus1c\result.json `
  --snapshot-date 2025-07-18
```

Через Compose (образ импортёра без внешних зависимостей):

```powershell
docker compose --profile import run --rm `
  -v "${PWD}/chats_history/1393071168_mssqlplus1c:/data/import:ro" `
  import --file /data/import/result.json --snapshot-date 2025-07-18
```

## 4. Несколько снимков одного чата

Каждая дата — отдельный индекс:

- `1393071168_mssqlplus1c_2025-07-18`
- `1393071168_mssqlplus1c_2026-09-17`

Алиас `{chat_id}_{slug}` (пример: `1393071168_mssqlplus1c`) всегда указывает
на **последний успешно импортированный** снимок этого чата.

Искать по конкретному снимку:

```text
GET 1393071168_mssqlplus1c_2025-07-18/_search
```

Искать по последнему снимку:

```text
GET 1393071168_mssqlplus1c/_search
```

Список всех загруженных чатов:

```text
GET kb_catalog/_search
```

В `kb_catalog` для каждого индекса есть `chat_name`, `snapshot_date`,
`message_count`, `date_min`, `date_max`, `alias`.

## 5. Поиск в Dashboards

1. http://127.0.0.1:5601 → войти как `admin`.
2. Stack Management → Index Patterns → создать:
   - `kb_catalog` — реестр чатов;
   - `1393071168_mssqlplus1c_*` — все снимки одного чата;
   - или `*_????-??-??` — все чаты.
3. Discover: поле `text`, фильтры `user_name.keyword`, `has_logs`,
   `timestamp`.

Пример REST:

```powershell
$password = ((Get-Content .env | Where-Object { $_ -match '^OPENSEARCH_INITIAL_ADMIN_PASSWORD=' }) -split '=',2)[1]
curl.exe -sk -u "admin:$password" -H "Content-Type: application/json" `
  -d "{\"query\":{\"match\":{\"text\":\"tempdb\"}}}" `
  https://127.0.0.1:9200/1393071168_mssqlplus1c/_search
```
