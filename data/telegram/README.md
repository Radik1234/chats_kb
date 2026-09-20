# Каталог выгрузок Telegram

Оператор кладёт JSON-экспорт Telegram Desktop **только в `inbox`**.
Имена чатов и маршрут файлов описаны в [docs/import-telegram.md](../../docs/import-telegram.md).

```text
data/telegram/
  inbox/{chat_id}_{slug}/       ← сюда
  processing/{chat_id}_{slug}/  ← сервис, не трогать
  archive/{chat_id}_{slug}/     ← успешно обработано
  failed/{chat_id}_{slug}/      ← ошибка
```

Пример: `inbox/1393071168_mssqlplus1c/result.json`
