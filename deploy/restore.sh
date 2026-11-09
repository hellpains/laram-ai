#!/usr/bin/env bash
# Восстановление базы из резервной копии.
# Запуск из папки проекта: sudo bash deploy/restore.sh backups/calls-ДАТА.db.gz
set -euo pipefail

cd "$(dirname "$0")/.."
BACKUP="${1:-}"
[[ -f "$BACKUP" ]] || { echo "Укажите файл копии: sudo bash deploy/restore.sh backups/calls-ДАТА.db.gz"; exit 1; }

TMP="$(mktemp --suffix=.db)"
trap 'rm -f "$TMP"' EXIT
gunzip -c "$BACKUP" > "$TMP"

echo "==> Текущая база будет заменена копией $BACKUP"
read -rp "Продолжить? [y/N] " ans
[[ "${ans,,}" == "y" ]] || exit 1

echo "==> Сохраняю текущую базу на всякий случай"
bash deploy/backup.sh || true

docker compose stop app
docker compose cp "$TMP" app:/data/calls.db
# Убираем журналы старой базы и возвращаем владельца файлу
docker compose run --rm --no-deps -u root app \
  sh -c "rm -f /data/calls.db-wal /data/calls.db-shm && chown callnotes:callnotes /data/calls.db"
docker compose start app
echo "==> Готово"
