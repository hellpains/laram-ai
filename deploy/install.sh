#!/usr/bin/env bash
# Установка CallNotes на чистый сервер Ubuntu 22.04 / 24.04.
# Запуск из папки проекта:  sudo bash deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Запустите через sudo: sudo bash deploy/install.sh"
[[ -f docker-compose.yml ]] || die "Запускайте из папки проекта callnotes"

# --- 1. Docker ---
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  say "Устанавливаю Docker"
  apt-get update -qq
  # Пакеты из репозитория Ubuntu: зеркала доступны из России без ограничений
  if ! apt-get install -y -qq docker.io docker-compose-v2; then
    warn "Пакет docker-compose-v2 не найден, ставлю Docker официальным скриптом"
    curl -fsSL https://get.docker.com | sh
  fi
  systemctl enable --now docker
fi

# Зеркало Docker Hub: базовые образы скачаются, даже если Docker Hub недоступен с сервера
if [[ ! -f /etc/docker/daemon.json ]]; then
  say "Настраиваю зеркало для образов Docker"
  mkdir -p /etc/docker
  printf '{\n  "registry-mirrors": ["https://mirror.gcr.io"]\n}\n' > /etc/docker/daemon.json
  systemctl restart docker
fi

# --- 2. Файрвол ---
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  say "Открываю порты 80 и 443"
  ufw allow 22/tcp >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

# --- 3. Настройки .env ---
set_env() {  # set_env KEY VALUE — заменить или дописать строку в .env
  local key="$1" value="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${value}|" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}
get_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi

DOMAIN="$(get_env DOMAIN)"
if [[ -z "$DOMAIN" ]]; then
  read -rp "Домен сервиса (например calls.example.ru): " DOMAIN
  [[ -n "$DOMAIN" ]] || die "Домен обязателен: без него не получить HTTPS-сертификат"
  set_env DOMAIN "$DOMAIN"
fi

if [[ -z "$(get_env APP_PASSWORD)" ]]; then
  set_env APP_PASSWORD "$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
fi
if [[ -z "$(get_env WEBHOOK_SECRET)" ]]; then
  set_env WEBHOOK_SECRET "$(openssl rand -hex 24)"
fi

# --- 4. Проверка DNS ---
say "Проверяю, что домен $DOMAIN указывает на этот сервер"
SERVER_IP="$(curl -fsS --max-time 5 https://api.ipify.org || true)"
DOMAIN_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
if [[ -z "$DOMAIN_IP" ]]; then
  warn "Домен $DOMAIN не найден в DNS. Создайте A-запись на ${SERVER_IP:-IP сервера} и запустите скрипт снова."
  exit 1
elif [[ -n "$SERVER_IP" && "$DOMAIN_IP" != "$SERVER_IP" ]]; then
  warn "Домен указывает на $DOMAIN_IP, а у сервера адрес $SERVER_IP."
  read -rp "Продолжить всё равно? [y/N] " ans
  [[ "${ans,,}" == "y" ]] || exit 1
fi

# --- 5. Запуск ---
say "Собираю и запускаю сервис (первый раз занимает 2–5 минут)"
docker compose up -d --build

say "Жду, пока сервис ответит по HTTPS"
ok=""
for _ in $(seq 1 40); do
  if curl -fsS --max-time 5 "https://$DOMAIN/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
if [[ -z "$ok" ]]; then
  warn "Сервис не ответил по https://$DOMAIN. Логи: docker compose logs --tail=50"
  exit 1
fi

# --- 6. Ежедневная резервная копия ---
CRON_LINE="30 3 * * * root cd $PROJECT_DIR && bash deploy/backup.sh >> /var/log/callnotes-backup.log 2>&1"
echo "$CRON_LINE" > /etc/cron.d/callnotes-backup
chmod 644 /etc/cron.d/callnotes-backup

# --- Итог ---
say "Готово"
cat <<EOF

  Интерфейс:        https://$DOMAIN
  Логин:            любой
  Пароль:           $(get_env APP_PASSWORD)

  Для Битрикс24 (исходящий вебхук, событие ONVOXIMPLANTCALLEND):
  Адрес обработчика: https://$DOMAIN/webhooks/bitrix

  Ключи Yandex и Битрикс24 впишите в $PROJECT_DIR/.env, затем:
    docker compose up -d
    docker compose exec app python -m scripts.setup_bitrix

  Резервная копия базы: каждый день в 03:30 в папку $PROJECT_DIR/backups
EOF

if [[ -z "$(get_env YANDEX_API_KEY)" ]]; then
  warn "Ключи Yandex не заданы — сервис работает в демо-режиме."
fi
