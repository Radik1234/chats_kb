# MCP для AI-агентов

В compose поднимается официальный сервер
[`opensearch-mcp-server-py`](https://github.com/opensearch-project/opensearch-mcp-server-py)
в режиме Streamable HTTP. Он ходит в OpenSearch стандартным REST (поиск,
маппинги, список индексов) — отдельный skills-сервис не используется.

Слушает только localhost: `http://127.0.0.1:9900/mcp`.

Учётные данные кластера MCP берёт из тех же переменных, что и остальной стек
(`OPENSEARCH_USERNAME` / `OPENSEARCH_INITIAL_ADMIN_PASSWORD`). Проверка
TLS к demo-сертификату OpenSearch отключена (`OPENSEARCH_SSL_VERIFY=false`).

## Cursor

Скопируйте пример и поправьте при необходимости:

```powershell
# фрагмент для ~/.cursor/mcp.json или настроек проекта
Get-Content .\config\cursor-mcp.example.json
```

```json
{
  "mcpServers": {
    "opensearch": {
      "url": "http://127.0.0.1:9900/mcp"
    }
  }
}
```

Сервис MCP не стартует вместе с кластером: нужен профиль и доступ Docker-демона к PyPI.

```powershell
docker compose --profile mcp up -d --build mcp
```

Если `docker pull` / `pip install` падают с `127.0.0.1:10809`, системный прокси Windows включён, а процесс на этом порту не слушает. Образы OpenSearch можно загрузить в обход демона:

```powershell
python .\scripts\pull_docker_image.py opensearchproject/opensearch:3.8.0 opensearchproject/opensearch-dashboards:3.8.0
```

После подключения у агента появляются инструменты OpenSearch (`ListIndex`,
`SearchIndex` и др.). Имеет смысл сначала смотреть `kb_catalog`, затем
искать в индексе `{chat_id}_{slug}` или в конкретном месяце
`{chat_id}_{slug}_{YYYY-MM}` (день — фильтр по `message_date`).

## Проверка, что порт жив

```powershell
Test-NetConnection 127.0.0.1 -Port 9900
```
