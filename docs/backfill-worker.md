# Фоновая дозагрузка чатов (importer-worker)

Сервис `importer` по расписанию, **в один поток**, дозагружает новые сообщения
уже известных чатов из Telegram и кладёт их в `data/telegram/inbox/{chat_id}_{slug}/`,
откуда их забирает существующий `ingest`. Разворачивается рядом с `ingest`,
использует тот же volume `./data/telegram`.

## Как это работает

Один цикл воркера (`services/importer/backfill_worker.py`):

1. **Single-flight.** Если в `processing/` (по умолчанию и в `inbox/`) есть
   `*.json` — цикл пропускается: новый импорт не стартует, пока предыдущий не
   уехал в `archive/`. Плюс межпроцессный лок `state/backfill.lock` (`flock`).
2. **Список чатов — из OpenSearch.** Берутся алиасы `{chat_id}_{slug}`
   (`_cat/aliases`), схема индексов (дневная/месячная) роли не играет.
3. **Приоритет — самый отстающий.** Для каждого чата считается `date_max`
   (`max(timestamp)`); в работу берётся чат с наименьшим `date_max`. Недавно
   проверенные чаты пропускаются по backoff (`BACKFILL_MIN_RECHECK_SEC`), чтобы
   не опрашивать неактивные вхолостую.
4. **Граница — по `message_id`.** Для выбранного чата берётся `max(message_id)`
   и передаётся как `min_id` в `iter_messages(entity, min_id=…, reverse=True)`.
   Это точная монотонная граница: «хвост» последнего (неполного) дня не теряется
   из-за дневной идемпотентности `ingest`.
5. **Окно — `BACKFILL_DAYS_PER_RUN` дней** вперёд от `date_max`. За один прогон —
   один чат, одно окно, один файл в `inbox` (формат Telegram Desktop export).
6. Обновляется состояние (`state/backfill_state.json`: `last_checked_at`,
   `last_max_id`), пауза `BACKFILL_INTERVAL_SEC`, следующий цикл.

Дозагружаются только чаты, уже присутствующие в индексах. Первичный «seed»
нового чата — отдельный путь (ручной экспорт в `inbox/`).

## Секреты и первичный вход

Backfill истории группы возможен только под **пользовательской** MTProto-сессией
(Bot API так не умеет). Нужны `api_id`/`api_hash` с https://my.telegram.org и
строковая сессия. Получить сессию один раз:

```bash
pip install "Telethon>=1.40,<2"
TG_API_ID=... TG_API_HASH=... python scripts/tg_login.py
# скопировать напечатанный StringSession в TG_SESSION (секрет, не коммитить)
```

Аккаунт должен состоять в дозагружаемых чатах. Не спамьте — при частых запросах
Telegram отдаёт FloodWait (воркер авто-ждёт паузы < 60 с через
`flood_sleep_threshold`).

## Переменные окружения

| Переменная | По умолч. | Назначение |
| --- | --- | --- |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` | — | Учётные данные user-сессии (секреты) |
| `BACKFILL_DAYS_PER_RUN` | `7` | Сколько дней истории тянуть за один прогон |
| `BACKFILL_INTERVAL_SEC` | `300` | Пауза между циклами |
| `BACKFILL_MIN_RECHECK_SEC` | `3600` | Не опрашивать один чат чаще, чем раз в N секунд |
| `BACKFILL_GUARD_INBOX` | `true` | Пропускать цикл, если в `inbox/` уже есть файл |

## Запуск

Сервис под профилем `backfill` (не стартует с обычным `up`, т.к. нужны секреты):

```bash
docker compose --profile backfill up -d --build importer
docker compose logs -f importer
```

Остановить: `docker compose stop importer`.
