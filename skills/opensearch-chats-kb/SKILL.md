---
name: "opensearch-chats-kb"
description: "Поиск и анализ chats_kb на VPS: скрипт kb-search.py, схема полей, рецепты запросов и анализа"
---

# Работа с базой знаний chats_kb (OpenSearch на VPS)

## Контекст

Локальный стек на VPS: OpenSearch 3.8 + Dashboards + ingest + MCP (docker compose).
- Репозиторий и .env: `/opt/chats_kb` (compose, конфиги)
- Данные кластера: docker volume `chats_kb_opensearch-data`
- Индексы: месячные `{chat_id}_{slug}_{YYYY-MM}`, алиас чата `{chat_id}_{slug}`, реестр месяцев `kb_catalog` (поле `month`, список дней в поле `days`). Дневная схема `{...}_{YYYY-MM-DD}` мигрирована 2026-09-21 (3417 дневных индексов → 173 месячных); день выборки — фильтр по `message_date`, НЕ имя индекса. Миграция повторно безопасна: `_id` сохраняется.
- Пароль: читать из `/opt/chats_kb/.env` (OPENSEARCH_INITIAL_ADMIN_PASSWORD), секреты нигде не дублировать
- REST: `https://127.0.0.1:9200` с `-k` (demo-сертификат); Dashboards: `http://127.0.0.1:5601`

## Главное правило: инструмент выбора

- Агент с exec → **скрипт `scripts/kb-search.py`** (или прямой REST на :9200). НЕ MCP.
- MCP (`http://127.0.0.1:9900/mcp`) — для внешних агентов без shell (Cursor и т.п.). Для себя не использовать: та же функциональность, больше накладных расходов (JSON-RPC-конверт), меньше гибкости (нет произвольного DSL и агрегаций).

## Схема полей документа (проверена на живых индексах)

```
chat_id, chat_name, slug, chat_type      — чат
message_date (YYYY-MM-DD), message_id    — день и id
timestamp (ISO)                          — поле времени для range/сортировки
user_id, user_name                       — автор (НЕ "from"!)
text                                     — текст сообщения
has_links, has_logs, has_photo, is_bot   — флаги
error_codes, media_type, file_name       — прочее
forwarded_from, via_bot                  — пересылки
```

## kb-search.py — команды

```bash
python3 scripts/kb-search.py chats                  # список чатов + счётчики
python3 scripts/kb-search.py stat                   # сводка кластера
python3 scripts/kb-search.py catalog --chat 1393071168   # реестр месяцев
python3 scripts/kb-search.py search "журнал регистрации" --chat 1393071168 --size 10
    # опции: --date-from/--date-to YYYY-MM-DD, --author "Имя", --phrase (точная фраза)
```

Вывод: `YYYY-MM-DD HH:MM | автор | текст` — без _index/_score/_source-обёрток.
Экономия токенов ~5-10x против сырого REST. Возвращает #hits, потом отсортированные по времени хиты.

## Рецепты анализа (когда kb-search.py недостаточно — прямой REST)

Пароль: `PW=$(grep '^OPENSEARCH_INITIAL_ADMIN_PASSWORD=' /opt/chats_kb/.env | cut -d= -f2-)`

Активность по месяцам (агрегация):
```bash
curl -sk -u "admin:$PW" -X POST "https://127.0.0.1:9200/{алиас}/_search" -H 'Content-Type: application/json' \
  -d '{"size":0,"aggs":{"months":{"date_histogram":{"field":"timestamp","calendar_interval":"month"}}}}'
```

Топ-авторы: `terms` по `user_name.keyword`, size 20.
Активность конкретного автора: `match` по `user_name` + `date_histogram`.
День целиком: фильтр по `message_date` на алиасе или месячном индексе, сортировка по `timestamp`:
`GET {алиас}/_search {"query":{"term":{"message_date":"2026-09-17"}}}` (см. событие 2020-01-22: 100 хитов — проверено).
Пользователь спрашивает «когда обсуждали X»: `--phrase` сначала, затем читаем контекст дня.

## Операционное

- Импорт нового чата: JSON из Telegram Desktop → `/opt/chats_kb/data/telegram/inbox/{chat_id}_{slug}/` (chat_id = поле id внутри JSON). Watcher сам: inbox→processing→archive/failed.
- Импорт идемпотентен по дням. Скорость ~8 с/день при интервале 5 с.
- Статус: `docker logs -f onec-kb-ingest`; ошибки → `data/telegram/failed/`.
- `vm.max_map_count` на хосте уже 1048576 — не трогать.
- Heap OpenSearch 2g; при импорте следить за `free -h` (машина 7.8G RAM, без swap).
- Пароль меняется только на первом старте пустого volume; `docker compose down -v` удаляет индексы.
- Бэкапов НЕТ — известный пробел пайплайна, при работе с ценными данными не делать down -v.
