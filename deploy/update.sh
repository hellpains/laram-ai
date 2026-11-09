#!/usr/bin/env bash
# Обновление после замены файлов проекта новой версией.
# Запуск из папки проекта: sudo bash deploy/update.sh
set -euo pipefail

cd "$(dirname "$0")/.."
echo "==> Резервная копия перед обновлением"
bash deploy/backup.sh
echo "==> Пересобираю и перезапускаю"
docker compose up -d --build
docker image prune -f >/dev/null
echo "==> Готово. Проверка: docker compose ps"
docker compose ps
