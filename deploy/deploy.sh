#!/usr/bin/env bash
# Запускается на сервере из GitHub Actions после загрузки кода.
# Собирает новый образ, перезапускает бота, проверяет, что он жив,
# и откатывается на предыдущую версию, если бот падает.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE=wildfinance-bot
CONTAINER=wildfinance-bot
CHECK_SECONDS=${CHECK_SECONDS:-25}

if [ ! -f .env ]; then
  echo "❌ Нет файла .env в $(pwd). Создай его по образцу .env.example." >&2
  exit 1
fi
mkdir -p data

# Запоминаем текущую рабочую версию для отката
HAS_PREVIOUS=0
if docker image inspect "$IMAGE:latest" >/dev/null 2>&1; then
  docker tag "$IMAGE:latest" "$IMAGE:previous"
  HAS_PREVIOUS=1
fi

echo "🔨 Сборка образа..."
docker compose build --pull

echo "🚀 Перезапуск бота..."
docker compose up -d --remove-orphans

echo "⏳ Проверка ${CHECK_SECONDS} сек..."
sleep "$CHECK_SECONDS"
STATE=$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo "false 0")

if [ "$STATE" != "true 0" ]; then
  echo "❌ Бот не запустился (running/restarts: $STATE). Последние логи:" >&2
  docker logs --tail 60 "$CONTAINER" >&2 || true
  if [ "$HAS_PREVIOUS" = "1" ]; then
    echo "↩️  Откат на предыдущую версию..." >&2
    docker tag "$IMAGE:previous" "$IMAGE:latest"
    docker compose up -d --no-build --force-recreate
  fi
  exit 1
fi

docker image prune -f >/dev/null
echo "✅ Деплой успешен. Бот работает."
docker logs --tail 5 "$CONTAINER"
