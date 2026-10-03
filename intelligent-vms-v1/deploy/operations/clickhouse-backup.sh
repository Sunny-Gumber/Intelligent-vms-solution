#!/usr/bin/env bash
set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
: "${CLICKHOUSE_BACKUP_DISK:?CLICKHOUSE_BACKUP_DISK is required}"

backup_id="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
port="${CLICKHOUSE_NATIVE_PORT:-9000}"
user="${CLICKHOUSE_USER:-default}"
config_file="${CLICKHOUSE_CONFIG_FILE:-}"

if [[ ! "$CLICKHOUSE_DATABASE" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "CLICKHOUSE_DATABASE contains unsupported characters" >&2
  exit 2
fi
if [[ ! "$CLICKHOUSE_BACKUP_DISK" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "CLICKHOUSE_BACKUP_DISK contains unsupported characters" >&2
  exit 2
fi
if [[ ! "$backup_id" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "BACKUP_ID contains unsupported characters" >&2
  exit 2
fi

args=(--host "$CLICKHOUSE_HOST" --port "$port" --user "$user")
if [[ -n "$config_file" ]]; then
  args+=(--config-file "$config_file")
fi

query="BACKUP DATABASE \`$CLICKHOUSE_DATABASE\` TO Disk('$CLICKHOUSE_BACKUP_DISK', '$backup_id')"
clickhouse-client "${args[@]}" --query "$query"
echo "clickhouse_backup_ok id=$backup_id database=$CLICKHOUSE_DATABASE disk=$CLICKHOUSE_BACKUP_DISK"
