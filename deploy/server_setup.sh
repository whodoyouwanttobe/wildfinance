#!/usr/bin/env bash
# Разовая подготовка VPS (Ubuntu 22.04/24.04 или Debian 12).
# Запуск от root:
#   bash server_setup.sh                 # ключ для GitHub сгенерируется сам
#   bash server_setup.sh "ssh-ed25519 AAAA..."   # или передай свой публичный ключ
# В конце скрипт выведет значения для секретов GitHub Actions.
set -euo pipefail

DEPLOY_PUBKEY="${1:-}"
DEPLOY_PRIVKEY_FILE=""
APP_DIR=/opt/wildfinance
DEPLOY_USER=deploy

if [ "$(id -u)" -ne 0 ]; then echo "Запусти от root (sudo -i)"; exit 1; fi
if [ -z "$DEPLOY_PUBKEY" ]; then
  DEPLOY_PRIVKEY_FILE=/root/github_deploy_key
  if [ ! -f "$DEPLOY_PRIVKEY_FILE" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "github-actions-deploy" -f "$DEPLOY_PRIVKEY_FILE"
  fi
  DEPLOY_PUBKEY=$(cat "$DEPLOY_PRIVKEY_FILE.pub")
fi

echo "📦 Обновление системы и установка Docker..."
apt-get update -y
apt-get install -y ca-certificates curl rsync ufw openssh-client
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

echo "👤 Пользователь $DEPLOY_USER..."
id "$DEPLOY_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"
AUTH="/home/$DEPLOY_USER/.ssh/authorized_keys"
touch "$AUTH"
grep -qxF "$DEPLOY_PUBKEY" "$AUTH" || echo "$DEPLOY_PUBKEY" >> "$AUTH"
chown "$DEPLOY_USER:$DEPLOY_USER" "$AUTH"; chmod 600 "$AUTH"

echo "📁 Папка приложения $APP_DIR..."
install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR" "$APP_DIR/data"

# Файрвол не трогаем по умолчанию: на сервере могут жить другие сервисы.
# Включить: ENABLE_UFW=1 bash server_setup.sh  (откроет только текущий SSH-порт)
if [ "${ENABLE_UFW:-0}" = "1" ]; then
  P=$({ sshd -T 2>/dev/null || true; } | awk '/^port /{print $2; exit}'); P=${P:-22}
  echo "🧱 Файрвол: разрешаю SSH-порт $P и включаю ufw..."
  ufw allow "$P/tcp" >/dev/null
  ufw --force enable >/dev/null
fi

echo
# .env, заранее загруженный в /root/.env
if [ -f /root/.env ] && [ ! -f "$APP_DIR/.env" ]; then
  mv /root/.env "$APP_DIR/.env"
  echo "🔑 .env перенесён в $APP_DIR/.env"
fi
if [ -f "$APP_DIR/.env" ]; then
  chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"
else
  echo "⚠️  $APP_DIR/.env ещё нет — загрузи его перед первым деплоем."
fi

PUBLIC_IP=${PUBLIC_IP:-$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}' || true)}
SSH_PORT_NOW=$({ sshd -T 2>/dev/null || true; } | awk '/^port /{print $2; exit}')
SSH_PORT_NOW=${SSH_PORT_NOW:-22}
KNOWN=""
for f in /etc/ssh/ssh_host_*_key.pub; do
  if [ "$SSH_PORT_NOW" = "22" ]; then host="$PUBLIC_IP"; else host="[$PUBLIC_IP]:$SSH_PORT_NOW"; fi
  KNOWN+="$host $(cut -d' ' -f1,2 "$f")"$'\n'
done

echo
echo "✅ Сервер готов."
echo
echo "════════ Секреты для GitHub (Settings → Secrets and variables → Actions) ════════"
echo "SSH_HOST  = $PUBLIC_IP"
echo "SSH_USER  = $DEPLOY_USER"
echo "SSH_PORT  = $SSH_PORT_NOW"
echo
echo "SSH_KNOWN_HOSTS (все строки целиком):"
printf "%s" "$KNOWN"
if [ -n "$DEPLOY_PRIVKEY_FILE" ]; then
  echo
  echo "SSH_PRIVATE_KEY (всё, включая строки BEGIN/END):"
  cat "$DEPLOY_PRIVKEY_FILE"
  echo
  echo "После того как вставишь ключ в GitHub, удали его с сервера:  rm $DEPLOY_PRIVKEY_FILE"
fi
echo "═════════════════════════════════════════════════════════════════════════════════"
