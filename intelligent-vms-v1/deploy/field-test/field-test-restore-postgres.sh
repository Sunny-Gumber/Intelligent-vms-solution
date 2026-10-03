#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
ENV_FILE="${VMS_ENV_FILE:-$ROOT/.env}"; : "${BACKUP_FILE:?BACKUP_FILE is required}"
[[ "${CONFIRM_RESTORE:-}" == "YES" ]] || { echo "set CONFIRM_RESTORE=YES to restore PostgreSQL" >&2; exit 3; }
[[ -f "$BACKUP_FILE" ]] || { echo "backup file not found" >&2; exit 2; }
dir="$(cd "$(dirname "$BACKUP_FILE")" && pwd)"; sum="$dir/$(basename "$BACKUP_FILE").sha256"
[[ -f "$sum" ]] && (cd "$dir" && sha256sum -c "$(basename "$sum")")
COMPOSE=(docker compose --env-file "$ENV_FILE")
"${COMPOSE[@]}" stop control-api onvif-event-worker alarm-worker placement-controller event-writer || true
"${COMPOSE[@]}" up -d --wait postgres
cat "$BACKUP_FILE" | "${COMPOSE[@]}" exec -T postgres pg_restore -U vms -d vms --clean --if-exists --no-owner --no-privileges
"${COMPOSE[@]}" run --rm --no-deps control-api alembic upgrade head
"${COMPOSE[@]}" up -d --wait
curl -fsS http://127.0.0.1:8000/api/v1/system/healthz/ready >/dev/null
echo "field_test_postgres_restore_ok; recording media was not modified"
