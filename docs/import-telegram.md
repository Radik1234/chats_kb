# Импорт Telegram Desktop JSON

## Экспорт

В Telegram Desktop выберите экспорт истории группы в формате JSON. Не
переименовывайте внутренние поля `messages`, `id`, `date`, `from_id`,
`reply_to_message_id` и `text`. Поле `text` может быть строкой или составным
массивом — импортёр поддерживает оба варианта.

Экспорт содержит персональные данные. Храните его вне репозитория, например в
игнорируемом каталоге `export/`, и не прикладывайте к issue или backup
конфигурации.

## Импорт через UI

1. Откройте `https://atlas.localhost`.
2. В мастере импорта выберите Telegram `result.json`.
3. Проверьте preview: источник Telegram, имя канала, количество и примеры.
4. Подтвердите mapping и commit.
5. Запустите sync/индексацию и дождитесь завершения extraction status.

Импорт двухпроходно вычисляет корни reply-цепочек. Повторный commit того же
экспорта идемпотентен по `(source_id, channel_id, message_id)` и обновляет, а
не дублирует сообщения.

## Импорт через API

Ключ берите локально из `.env`, не вставляйте его в документацию или Git.

```powershell
$key = ((Get-Content .env | Where-Object { $_ -match '^BEEVER_API_KEYS=' }) -split '=',2)[1]
curl.exe -k -H "Authorization: Bearer $key" `
  -F "file=@export\result.json;type=application/json" `
  https://atlas.localhost/api/imports/preview
```

Ответ preview содержит `file_id` и `mapping`. Передайте их в
`POST /api/imports/commit` вместе с `channel_name`; удобнее сделать это UI,
чтобы не ошибиться в JSON mapping. Staging имеет TTL, поэтому commit следует
выполнить сразу после preview.

## Проверка

```powershell
.\scripts\smoke-test.ps1
```

В UI проверьте keyword search, фильтры автора/дат и переход к контексту.
Semantic search ищет извлечённые факты, поэтому становится полным после
завершения локального extraction. Не отправляйте массовый экспорт в extraction
без контроля очереди: Qwen 14B обрабатывает сообщения последовательно и это
существенно дольше простого импорта.
