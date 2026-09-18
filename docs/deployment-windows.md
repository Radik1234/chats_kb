# Развёртывание на Windows

Нужны Docker Desktop 24+ (Compose v2) и WSL2. Рекомендуется отдать Docker не
меньше 8 ГБ RAM; на этой машине достаточно запаса.

## 1. Параметры хоста

OpenSearch требует увеличенный `vm.max_map_count`. Скрипт `bootstrap.ps1`
делает это сам. Вручную:

```powershell
wsl -d docker-desktop -e sysctl -w vm.max_map_count=262144
```

Значение сбрасывается после перезапуска Docker Desktop — при ошибке
`max virtual memory areas vm.max_map_count` повторите команду.

## 2. Секреты

```powershell
Copy-Item .env.example .env
```

`OPENSEARCH_INITIAL_ADMIN_PASSWORD` должен быть сложным (заглавная, строчная,
цифра, спецсимвол, без словарных фраз). Пароль задаётся **только при первом
старте** пустого volume. Смена пароля в `.env` у уже созданного кластера его
не меняет: либо оставьте прежний, либо `docker compose down -v` (удалит
индексы) и поднимите стек заново.

Не коммитьте `.env`.

## 3. Запуск

```powershell
Set-Location F:\AI\Projects\onec_kb
.\scripts\bootstrap.ps1
docker compose ps
```

Сервисы:

| Сервис | URL | Назначение |
| --- | --- | --- |
| OpenSearch | https://127.0.0.1:9200 | REST, индексы, поиск |
| Dashboards | http://127.0.0.1:5601 | UI поиска и Discover |
| MCP | http://127.0.0.1:9900/mcp | инструменты для AI-агентов |

Сертификат OpenSearch — demo/self-signed. Для `curl` используйте `-k`.

Проверка API:

```powershell
$password = ((Get-Content .env | Where-Object { $_ -match '^OPENSEARCH_INITIAL_ADMIN_PASSWORD=' }) -split '=',2)[1]
curl.exe -sk -u "admin:$password" https://127.0.0.1:9200
```

В Dashboards войдите как `admin` с тем же паролем. Создайте index pattern
`kb_catalog` (каталог чатов) и `*_????-??-??` или конкретное имя индекса
чата, например `1393071168_mssqlplus1c_*`.

## 4. Остановка

```powershell
docker compose stop          # данные в volume сохраняются
docker compose down          # контейнеры удаляются, volume остаётся
# docker compose down -v     # УДАЛИТ индексы
```

## 5. Если не стартует

1. `docker compose logs opensearch --tail 200`
2. Пароль слишком слабый — OpenSearch сразу выходит; смените пароль в `.env`
   **до** первого успешного старта.
3. `vm.max_map_count` — см. выше.
4. Порты 9200/5601/9900 заняты другим процессом.
5. `docker pull` пишет `connecting to 127.0.0.1:10809` — см. [MCP / прокси](mcp-setup.md). Образы можно загрузить скриптом `scripts/pull_docker_image.py`.
