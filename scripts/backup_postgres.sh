#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/standup/app"
BACKUP_DIR="/home/standup/backups/postgres"
DB_NAME="standup_db"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.dump"

set -a
source "${APP_DIR}/.env"
set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

pg_dump "$DATABASE_URL" -F c -f "$BACKUP_FILE"
chmod 600 "$BACKUP_FILE"

find "$BACKUP_DIR" -type f -name "${DB_NAME}_*.dump" -mtime +14 -delete

echo "Backup created: $BACKUP_FILE"
