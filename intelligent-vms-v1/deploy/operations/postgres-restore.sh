#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_FILE:?BACKUP_FILE is required}"

if [[ -n "${PGSERVICE:-}" ]]; then
  restore_target="service=${PGSERVICE}"
elif [[ -n "${PGDATABASE:-}" ]]; then
  restore_target="${PGDATABASE}"
else
  echo "configure libpq with PGSERVICE or PGDATABASE/PGHOST/PGUSER (and PGPASSFILE as needed)" >&2
  exit 2
fi

if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "backup file does not exist" >&2
  exit 2
fi

backup_dir="$(cd "$(dirname "$BACKUP_FILE")" && pwd)"
backup_name="$(basename "$BACKUP_FILE")"
checksum_file="$backup_dir/${backup_name}.sha256"

if [[ -f "$checksum_file" ]]; then
  (
    cd "$backup_dir"
    sha256sum -c "$(basename "$checksum_file")"
  )
fi

pg_restore --list "$BACKUP_FILE" >/dev/null

if [[ "${VALIDATE_ONLY:-0}" == "1" ]]; then
  echo "postgres_restore_validation_ok"
  exit 0
fi

if [[ "${CONFIRM_RESTORE:-}" != "YES" ]]; then
  echo "set CONFIRM_RESTORE=YES to perform the destructive PostgreSQL restore" >&2
  exit 3
fi

pg_restore \
  --dbname="$restore_target" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  "$BACKUP_FILE"

echo "postgres_restore_ok"
