# Backup и восстановление

## Что сохраняется

`backup.ps1` на короткое время останавливает writers и четыре data services,
после чего создаёт согласованные cold snapshots:

- MongoDB (`mongo_data`);
- Neo4j (`neo4j_data`);
- Weaviate (`weaviate_data`);
- Redis (`redis_data`);
- `.env`, Compose/Caddy/Atlas config, MCP example и Atlas patchset.

Для каждого файла записываются размер и SHA-256 в `manifest.json`. Backup
содержит секреты из `.env`, поэтому каталог `backups/` игнорируется Git и
должен храниться с ограниченным доступом.

## Создание

```powershell
.\scripts\backup.ps1
```

Можно указать каталог:

```powershell
.\scripts\backup.ps1 -Destination F:\PrivateBackups\onec-kb-20260907
```

Скрипт сохраняет набор ранее запущенных сервисов и поднимает его снова. Ключ
`-KeepStopped` оставляет остановленные компоненты остановленными.

## Восстановление

Restore проверяет все SHA-256 до изменения volumes. Операция полностью заменяет
данные целевого Compose project; сначала остановите запись и убедитесь, что
выбран правильный backup.

```powershell
.\scripts\restore.ps1 `
  -BackupPath F:\PrivateBackups\onec-kb-20260907 `
  -Confirm:$false
docker compose up -d --wait
.\scripts\smoke-test.ps1
```

Конфигурация по умолчанию не перезаписывается. Для аварийного восстановления
также `.env` и config добавьте `-RestoreConfig`; перед этим сохраните текущую
конфигурацию отдельно.

Архивы являются снимками файлов data volumes и совместимы с закреплёнными
версиями образов из `docker-compose.yml`. Не восстанавливайте их поверх другой
мажорной версии MongoDB/Neo4j/Weaviate/Redis.

## Безопасная проверка в изоляции

```powershell
$project = "onec-kb-restore-test"
.\scripts\restore.ps1 -BackupPath F:\PrivateBackups\onec-kb-20260907 `
  -ProjectName $project -Confirm:$false
docker compose -p $project up -d --wait mongodb neo4j weaviate redis
docker compose -p $project exec -T mongodb mongosh --quiet beever_atlas `
  --eval "db.channel_messages.countDocuments({})"
docker compose -p $project exec -T redis redis-cli DBSIZE
docker compose -p $project down
docker volume rm "${project}_mongo_data" "${project}_neo4j_data" `
  "${project}_weaviate_data" "${project}_redis_data"
```

У изолированных volumes нет Compose labels, поэтому после теста они удаляются
явно. Никогда не подставляйте имя рабочего project в команду удаления.
