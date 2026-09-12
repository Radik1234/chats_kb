# Подключение Atlas MCP к Cursor

MCP доступен только через `https://atlas.localhost/mcp`, использует
Streamable HTTP и отдельный bearer key `BEEVER_MCP_API_KEYS`.

1. Убедитесь, что локальный CA Caddy доверен согласно
   [deployment-windows.md](deployment-windows.md).
2. Скопируйте структуру из `config/cursor-mcp.example.json` в MCP-настройки
   Cursor.
3. Замените `<BEEVER_MCP_API_KEY>` значением `BEEVER_MCP_API_KEYS` из
   локального `.env`. Не изменяйте и не коммитьте файл-пример с реальным ключом.
4. Перезапустите MCP server в Cursor и проверьте список tools.

Пример без секрета:

```json
{
  "mcpServers": {
    "atlas-local": {
      "url": "https://atlas.localhost/mcp",
      "headers": {
        "Authorization": "Bearer <BEEVER_MCP_API_KEY>"
      }
    }
  }
}
```

Для приёмки вызовите `search_messages`, затем передайте найденный
`message_id` в tool получения контекста сообщения. Также проверьте поиск
фактов/wiki и структурированный ответ. MCP key не равен dashboard API key.

Диагностика:

```powershell
docker compose logs --tail 200 atlas
.\scripts\smoke-test.ps1
```

`401` означает отсутствующий/неверный MCP key; ошибка TLS обычно означает,
что локальный CA не импортирован. Не отключайте проверку TLS в постоянной
конфигурации Cursor.
