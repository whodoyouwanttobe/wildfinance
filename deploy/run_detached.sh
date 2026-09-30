#!/usr/bin/env bash
# Запускает deploy.sh в фоне, независимо от SSH-сессии.
# Если сервер оборвёт SSH-соединение посреди сборки, деплой всё равно
# доработает до конца. GitHub Actions потом опрашивает файл deploy.status.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f deploy.status deploy.log
setsid nohup bash -c 'bash deploy/deploy.sh > deploy.log 2>&1; echo $? > deploy.status' \
  >/dev/null 2>&1 < /dev/null &
echo "🚀 Деплой запущен в фоне (лог: $(pwd)/deploy.log)"
