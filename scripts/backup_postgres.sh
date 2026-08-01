#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/standup/app"
BACKUP_DIR="/home/standup/backups/postgres"
DB_NAME="standup_db"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_${TIMESTAMP}.dump"
RETENTION_DAYS=14

set -a
source "${APP_DIR}/.env"
set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

pg_dump "$DATABASE_URL" -F c -f "$BACKUP_FILE"
chmod 600 "$BACKUP_FILE"

find "$BACKUP_DIR" -type f -name "${DB_NAME}_*.dump" -mtime +"${RETENTION_DAYS}" -delete

echo "Backup created: $BACKUP_FILE"

upload_to_s3() {
  local endpoint="${S3_BACKUP_ENDPOINT:-}"
  local bucket="${S3_BACKUP_BUCKET:-}"
  local access_key="${S3_BACKUP_ACCESS_KEY:-}"
  local secret_key="${S3_BACKUP_SECRET_KEY:-}"

  if [[ -z "$endpoint" && -z "$bucket" && -z "$access_key" && -z "$secret_key" ]]; then
    echo "S3 backup skipped: S3_BACKUP_* not set in .env"
    return 0
  fi

  if [[ -z "$endpoint" || -z "$bucket" || -z "$access_key" || -z "$secret_key" ]]; then
    echo "S3 backup error: set S3_BACKUP_ENDPOINT, S3_BACKUP_BUCKET, S3_BACKUP_ACCESS_KEY, S3_BACKUP_SECRET_KEY" >&2
    return 1
  fi

  if ! command -v aws >/dev/null 2>&1; then
    echo "S3 backup error: aws CLI not installed (apt install awscli)" >&2
    return 1
  fi

  export AWS_ACCESS_KEY_ID="$access_key"
  export AWS_SECRET_ACCESS_KEY="$secret_key"
  export AWS_DEFAULT_REGION="${S3_BACKUP_REGION:-ru1}"
  export AWS_EC2_METADATA_DISABLED=true
  export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
  export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

  local aws_cfg
  aws_cfg="$(mktemp)"
  trap 'rm -f "$aws_cfg"' RETURN
  cat >"$aws_cfg" <<EOF
[default]
s3 =
    addressing_style = path
    signature_version = s3v4
EOF
  export AWS_CONFIG_FILE="$aws_cfg"

  local object_name
  object_name="$(basename "$BACKUP_FILE")"

  aws s3 cp "$BACKUP_FILE" "s3://${bucket}/${object_name}" --endpoint-url "$endpoint"
  echo "S3 upload ok: s3://${bucket}/${object_name}"

  local cutoff
  cutoff="$(date -d "${RETENTION_DAYS} days ago" +%Y%m%d)"

  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    local stamp
    stamp="$(echo "$key" | sed -n "s/^${DB_NAME}_\\([0-9]\\{8\\}\\)_.*/\\1/p")"
    if [[ -n "$stamp" && "$stamp" < "$cutoff" ]]; then
      aws s3 rm "s3://${bucket}/${key}" --endpoint-url "$endpoint"
      echo "S3 deleted old: ${key}"
    fi
  done < <(aws s3 ls "s3://${bucket}/" --endpoint-url "$endpoint" | awk '{print $4}' | grep -E "^${DB_NAME}_[0-9]{8}_[0-9]{6}\\.dump$" || true)
}

upload_to_s3
