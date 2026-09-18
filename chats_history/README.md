# Выгрузки Telegram

Кладём сюда JSON-экспорт Telegram Desktop. Имя папки задаёт slug индекса:

```text
chats_history/
  {telegram_chat_id}_{slug}/
    result.json
```

Пример: `1393071168_mssqlplus1c/result.json` → индекс
`1393071168_mssqlplus1c_YYYY-MM-DD`.

Если структура другая, slug можно передать явно: `.\scripts\import-chat.ps1 -Slug ...`.

Как экспортировать чат и импортировать его в OpenSearch — в
[docs/import-telegram.md](../docs/import-telegram.md).
