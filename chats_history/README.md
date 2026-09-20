# Исходные выгрузки (архив репозитория)

Здесь лежит эталонный JSON для отладки. Рабочий конвейер ingest **не читает**
этот каталог.

Чтобы загрузить чат:

1. Скопируйте файл в inbox:

```text
data/telegram/inbox/{telegram_chat_id}_{slug}/result.json
```

Пример:

```powershell
New-Item -ItemType Directory -Force data\telegram\inbox\1393071168_mssqlplus1c | Out-Null
Copy-Item chats_history\1393071168_mssqlplus1c\result.json data\telegram\inbox\1393071168_mssqlplus1c\
```

2. Дальше файлом занимается сервис `ingest` — см. [docs/import-telegram.md](../docs/import-telegram.md).
