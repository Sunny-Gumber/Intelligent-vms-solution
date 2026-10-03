#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_DIR:?BACKUP_DIR is required}"

if [[ -z "${PGSERVICE:-}" && -z "${PGDATABASE:-}" ]]; then
  echo "configure libpq with PGSERVICE or PGDATABASE/PGHOST/PGUSER (and PGPASSFILE as needed)" >&2
  exit 2
fi

umask 077
backup_id="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
target="${BACKUP_DIR%/}/${backup_id}/postgres"
mkdir -p "$target"

tmp="$target/database.dump.tmp"
dump="$target/database.dump"

rm -f "$tmp"
pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$tmp"

mv "$tmp" "$dump"
(
  cd "$target"
  sha256sum "database.dump" > "database.dump.sha256"
)
pg_restore --list "$dump" >/dev/null

printf 'postgres_backup_ok id=%s file=%s\n' "$backup_id" "$dump"
