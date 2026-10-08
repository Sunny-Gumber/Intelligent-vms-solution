#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
ENV_FILE="${VMS_ENV_FILE:-$ROOT/.env}"; : "${BACKUP_FILE:?BACKUP_FILE is required}"
[[ "${CONFIRM_RESTORE:-}" == "YES" ]] || { echo "set CONFIRM_RESTORE=YES to restore PostgreSQL" >&2; exit 3; }
[[ -f "$BACKUP_FILE" ]] || { echo "backup file not found" >&2; exit 2; }
dir="$(cd "$(dirname "$BACKUP_FILE")" && pwd)"; sum="$dir/$(basename "$BACKUP_FILE").sha256"
[[ -f "$sum" ]] && (cd "$dir" && sha256sum -c "$(basename "$sum")")
COMPOSE=(docker compose --env-file "$ENV_FILE")
# These services write PostgreSQL. A zero exit from `compose stop` is not proof
# they stopped: a container can still be running, restarting, or paused.
WRITER_SERVICES=(control-api onvif-event-worker alarm-worker placement-controller event-writer)
ACTIVE_WRITER_STATES=(running restarting paused)
if ! "${COMPOSE[@]}" stop "${WRITER_SERVICES[@]}"; then
  echo "field-test restore aborted: failed to stop writer services; pg_restore was not run" >&2
  exit 1
fi
status_filter=()
for state in "${ACTIVE_WRITER_STATES[@]}"; do
  status_filter+=(--status "$state")
done
still_running=()
for service in "${WRITER_SERVICES[@]}"; do
  if ! active_ids="$("${COMPOSE[@]}" ps "${status_filter[@]}" -q "$service")"; then
    echo "field-test restore aborted: unable to verify writer service ${service} is stopped; pg_restore was not run" >&2
    exit 1
  fi
  if [[ -n "${active_ids//[[:space:]]/}" ]]; then
    still_running+=("$service")
  fi
done
if [[ ${#still_running[@]} -gt 0 ]]; then
  echo "field-test restore aborted: writer containers still running: ${still_running[*]}; pg_restore was not run" >&2
  exit 1
fi
"${COMPOSE[@]}" up -d --wait postgres
cat "$BACKUP_FILE" | "${COMPOSE[@]}" exec -T postgres pg_restore -U vms -d vms --clean --if-exists --no-owner --no-privileges
"${COMPOSE[@]}" run --rm --no-deps control-api alembic upgrade head
"${COMPOSE[@]}" up -d --wait
curl -fsS http://127.0.0.1:8000/api/v1/system/healthz/ready >/dev/null
echo "field_test_postgres_restore_ok; recording media was not modified"
