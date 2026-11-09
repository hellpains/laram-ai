#!/usr/bin/env bash
# Резервная копия базы звонков. Запуск из папки проекта: bash deploy/backup.sh
# Хранит копии за KEEP_DAYS дней (по умолчанию 14) в папке backups/.
set -euo pipefail

cd "$(dirname "$0")/.."
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p backups
chmod 700 backups

# Онлайн-копия SQLite: безопасно, даже если в этот момент обрабатываются звонки
docker compose exec -T app python -c "
import sqlite3
src = sqlite3.connect('/data/calls.db')
dst = sqlite3.connect('/data/backup.db')
src.backup(dst)
dst.close(); src.close()
"
docker compose cp app:/data/backup.db "backups/calls-$STAMP.db"
docker compose exec -T app rm -f /data/backup.db
gzip "backups/calls-$STAMP.db"
chmod 600 "backups/calls-$STAMP.db.gz"

find backups -name 'calls-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') backup ok: backups/calls-$STAMP.db.gz"
