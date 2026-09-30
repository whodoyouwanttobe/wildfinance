#!/usr/bin/env bash
# Переезд со старого деплоя (из deploy.zip) на новый (GitHub + Docker Compose).
#
# Что делает:
#   1) Находит старого бота: Docker-контейнер, systemd-сервис или просто процесс python bot.py
#   2) Сохраняет его базу users.db (пользователи, триалы, оплаты) в /root/wf_backup/
#   3) С флагом --stop останавливает старого бота, чтобы он не конфликтовал с новым
#   4) Кладёт самую свежую базу в /opt/wildfinance/data/users.db (если там ещё пусто)
#
# Запуск от root:
#   bash migrate_from_zip.sh          # только найти и сделать бэкап (ничего не останавливает)
#   bash migrate_from_zip.sh --stop   # бэкап + остановить старого бота
set -uo pipefail

STOP=0
[ "${1:-}" = "--stop" ] && STOP=1
BACKUP=/root/wf_backup/$(date +%F_%H%M%S)
APP_DIR=/opt/wildfinance
mkdir -p "$BACKUP"
FOUND=0

if [ "$(id -u)" -ne 0 ]; then echo "Запусти от root (sudo -i)"; exit 1; fi

save_db() {  # $1 — путь к файлу, $2 — метка
  if [ -s "$1" ]; then
    cp -p "$1" "$BACKUP/users_$2.db"
    echo "   💾 база сохранена: $BACKUP/users_$2.db ($(stat -c %s "$1") байт, изменена $(stat -c %y "$1" | cut -d. -f1))"
  fi
}

echo "🔎 1. Docker-контейнеры со старым ботом..."
if command -v docker >/dev/null; then
  for c in $(docker ps -a --format '{{.Names}}'); do
    [ "$c" = "wildfinance-bot" ] && continue          # это уже новый деплой
    cmd=$(docker inspect -f '{{join .Config.Cmd " "}}' "$c" 2>/dev/null)
    case "$cmd" in *bot.py*) ;; *) continue ;; esac
    FOUND=1
    echo " • контейнер $c ($(docker inspect -f '{{.State.Status}}' "$c"))"
    for p in /app/data/users.db /app/users.db; do
      if docker cp "$c:$p" "$BACKUP/tmp.db" 2>/dev/null; then
        save_db "$BACKUP/tmp.db" "docker_${c}"; rm -f "$BACKUP/tmp.db"; break
      fi
    done
    if [ $STOP = 1 ]; then
      docker update --restart=no "$c" >/dev/null 2>&1
      docker stop "$c" >/dev/null && echo "   ⏹ остановлен (контейнер не удалён — можно вернуть: docker start $c)"
    fi
  done
fi

echo "🔎 2. systemd-сервисы с bot.py..."
for unit in $(systemctl list-units --type=service --all --no-legend 2>/dev/null | awk '{print $1}'); do
  exec_line=$(systemctl show -p ExecStart --value "$unit" 2>/dev/null)
  case "$exec_line" in *bot.py*) ;; *) continue ;; esac
  FOUND=1
  wd=$(systemctl show -p WorkingDirectory --value "$unit")
  echo " • сервис $unit (папка: ${wd:-?})"
  [ -n "$wd" ] && save_db "$wd/users.db" "systemd_${unit%.service}"
  if [ $STOP = 1 ]; then
    systemctl disable --now "$unit" >/dev/null 2>&1 && echo "   ⏹ остановлен и убран из автозапуска"
  fi
done

echo "🔎 3. Процессы python ... bot.py (screen/tmux/nohup)..."
for pid in $(pgrep -f 'python[0-9.]* .*bot\.py' 2>/dev/null); do
  # пропускаем процессы внутри Docker (их обработали выше)
  grep -qE 'docker|containerd' /proc/$pid/cgroup 2>/dev/null && continue
  FOUND=1
  cwd=$(readlink -f /proc/$pid/cwd)
  echo " • PID $pid, папка: $cwd"
  save_db "$cwd/users.db" "proc_$pid"
  if [ $STOP = 1 ]; then
    kill "$pid" && echo "   ⏹ процесс остановлен"
  fi
done

echo "🔎 4. Ищу другие users.db на диске..."
find / -xdev -name users.db -not -path "$BACKUP/*" -not -path "$APP_DIR/*" -not -path '/proc/*' 2>/dev/null |
  while read -r f; do echo " • $f"; save_db "$f" "file_$(echo "$f" | tr '/' '_')"; done

echo
if [ $FOUND = 0 ]; then
  echo "ℹ️  Запущенный старый бот не найден (возможно, он уже остановлен)."
fi

NEWEST=$(ls -t "$BACKUP"/users_*.db 2>/dev/null | head -1)
if [ -n "$NEWEST" ]; then
  echo "📦 Самая свежая база: $NEWEST"
  if [ -d "$APP_DIR" ]; then
    mkdir -p "$APP_DIR/data"
    if [ -s "$APP_DIR/data/users.db" ]; then
      echo "   $APP_DIR/data/users.db уже есть — не перезаписываю."
    else
      cp -p "$NEWEST" "$APP_DIR/data/users.db"
      chown -R deploy:deploy "$APP_DIR/data" 2>/dev/null || true
      echo "   ✅ скопирована в $APP_DIR/data/users.db"
    fi
  else
    echo "   Папки $APP_DIR ещё нет — сначала запусти server_setup.sh, потом этот скрипт ещё раз."
  fi
else
  echo "⚠️  Базу users.db не нашёл. Если пользователи важны — найди её вручную до остановки старого бота."
fi

echo
if [ $STOP = 0 ]; then
  echo "Это был пробный прогон: старый бот НЕ остановлен."
  echo "Когда будешь готов к переезду:  bash $0 --stop"
else
  echo "Старый бот остановлен. Теперь запускай деплой из GitHub (Actions → Run workflow)."
fi
echo "Бэкапы лежат в $BACKUP"
