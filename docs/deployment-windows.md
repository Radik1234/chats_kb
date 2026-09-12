# Развёртывание на Windows

## Требования

- Windows 10 x64, WSL2 и актуальный Docker Desktop с Linux containers;
- Docker Compose v2, Git, NVIDIA-драйвер с WSL/CUDA;
- около 25 ГиБ свободного места минимум;
- рекомендуется 48 ГиБ RAM для Docker/WSL перед длительной индексацией.

Текущий лимит Docker можно увидеть командой:

```powershell
docker info --format "{{.MemTotal}}"
```

Если доступно около 32 ГиБ, настройте 48 ГиБ в `%UserProfile%\.wslconfig`,
например:

```ini
[wsl2]
memory=48GB
```

Затем выполните `wsl --shutdown` и перезапустите Docker Desktop. Bootstrap
глобальные настройки сам не меняет.

## Установка

```powershell
Set-Location F:\AI\Projects\onec_kb
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1
.\scripts\download-models.ps1 -Model All
docker compose config --quiet
docker compose up -d
docker compose ps
.\scripts\smoke-test.ps1
```

`bootstrap.ps1` создаёт `.env` со случайными раздельными ключами, клонирует
закреплённый Atlas v0.2.0 и применяет tracked patchset. Существующий `.env`
не перезаписывается.

## Локальный TLS

Caddy публикует только `127.0.0.1:80/443`; MongoDB, Neo4j, Redis, Weaviate и
llama.cpp доступны лишь во внутренней Compose network.

```powershell
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .\caddy-local-root.crt
Import-Certificate .\caddy-local-root.crt -CertStoreLocation Cert:\CurrentUser\Root
Remove-Item .\caddy-local-root.crt
```

Импортируйте CA только на доверенном локальном компьютере. После этого UI/API
доступны по `https://atlas.localhost`.

## Обновление и диагностика

```powershell
docker compose logs --tail 200 atlas
docker compose logs --tail 200 llama-chat llama-embedding
docker stats --no-stream
nvidia-smi
.\scripts\smoke-test.ps1
```

Безопасный перезапуск, сохраняющий named volumes:

```powershell
docker compose restart
docker compose up -d --wait
```

Модели и embedding dimension закреплены в `.env.example`. Смена bge-m3 или
размерности 1024 после индексации требует полной переиндексации Weaviate.
