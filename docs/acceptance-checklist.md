# Итоговая приёмка

Дата проверки: 2026-09-07. Использовались только синтетические обезличенные
данные.

## Функциональность и эксплуатация

- [x] `docker compose up -d` поднимает закреплённый локальный стек.
- [x] Deep health возвращает healthy для MongoDB, Neo4j, Weaviate и Redis.
- [x] Наружу опубликован только Caddy на `127.0.0.1:80/443`; API/MCP идут по
  HTTPS, остальные сервисы находятся во внутренней network.
- [x] Анонимный запрос к `/api/channels` получает `401`.
- [x] Telegram Desktop `result.json` принимается напрямую; повторный импорт
  идемпотентен, reply-цепочки и временной контекст сохраняются.
- [x] Работают keyword/author/date search и контекст сырых сообщений.
- [x] Semantic retrieval использует локальные bge-m3 embeddings размерности
  1024; wiki, graph и structured Qwen flow проверены на синтетической fixture.
- [x] MCP использует Streamable HTTP с отдельным bearer key; доступны
  `search_messages` и получение контекста.
- [x] Smoke test проверяет Compose, health, закрытые порты, auth, API и оба
  локальных AI endpoints.

## Backup/restore

- [x] Создан cold snapshot MongoDB/Neo4j/Weaviate/Redis/config с 13
  проверенными SHA-256 файлами.
- [x] Backup восстановлен в отдельный project `onec-kb-acceptance`.
- [x] Из восстановленных volumes запущены четыре data services: все healthy.
- [x] Контроль после restore: 12 MongoDB messages, 16 Neo4j nodes, Redis
  отвечает (`DBSIZE=0` допустим для данного снимка), Weaviate ready.
- [x] Временные контейнеры/network/volumes удалены; рабочие volumes не
  изменялись.

## Нагрузочный тест retrieval

Методика:

- 100 000 детерминированных raw messages загружены в отдельный benchmark
  channel и проиндексированы MongoDB text index;
- semantic benchmark использовал репрезентативные 2 000 детерминированных
  фактов из тех же пяти тематик, локально векторизованных bge-m3;
- 5 warm-up + 50 последовательных измеряемых запросов для каждого режима;
- semantic latency включает создание query embedding и поиск Weaviate;
- 100k LLM extraction намеренно не запускался; benchmark rows удалены после
  теста (контрольный count равен 0).

Результаты:

- генерация JSON: 100 000 сообщений, 38 516 744 байт за 0,777 с;
- загрузка MongoDB: 23 391,6 сообщений/с (4,275 с);
- semantic indexing: 50,0 факта/с (40,017 с);
- keyword: p50 263,09 мс, **p95 284,95 мс**, max 319,42 мс;
- semantic: p50 30,95 мс, **p95 33,30 мс**, max 34,77 мс;
- критерий NFR-4 p95 ≤ 2–3 с выполнен с запасом.

Ресурсы после теста (`docker stats --no-stream`):

- Atlas 374 МиБ (лимит 4 ГиБ), MongoDB 607 МиБ, Weaviate 209 МиБ,
  Neo4j 744 МиБ;
- llama-chat 14,3 ГиБ container memory, llama-embedding 1,283 ГиБ;
- GPU RTX 5060 Ti: 13 001 / 16 311 МиБ VRAM, 19% utilization в момент снимка;
- Docker сейчас предоставляет 30,18 ГиБ, меньше рекомендованных 48 ГиБ.

## Ограничения

- Нагрузка измерена с concurrency=1 на синтетическом равномерном корпусе;
  это latency baseline, а не многопользовательский saturation test.
- Semantic sample содержит 2% raw corpus и обходит LLM extraction, поэтому
  измеряет retrieval path, но не качество/скорость извлечения 100k фактов.
- Cold backup создаёт короткое окно недоступности и требует тех же версий
  образов при restore.
- Перед длительной реальной индексацией нужно увеличить Docker/WSL memory до
  48 ГиБ.
