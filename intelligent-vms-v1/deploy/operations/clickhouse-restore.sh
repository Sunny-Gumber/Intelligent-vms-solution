#!/usr/bin/env bash
set -euo pipefail

: "${CLICKHOUSE_HOST:?CLICKHOUSE_HOST is required}"
: "${CLICKHOUSE_DATABASE:?CLICKHOUSE_DATABASE is required}"
: "${CLICKHOUSE_TARGET_DATABASE:?CLICKHOUSE_TARGET_DATABASE is required}"
: "${CLICKHOUSE_BACKUP_DISK:?CLICKHOUSE_BACKUP_DISK is required}"
: "${BACKUP_ID:?BACKUP_ID is required}"

port="${CLICKHOUSE_NATIVE_PORT:-9000}"
user="${CLICKHOUSE_USER:-default}"
config_file="${CLICKHOUSE_CONFIG_FILE:-}"

for value in "$CLICKHOUSE_DATABASE" "$CLICKHOUSE_TARGET_DATABASE"; do
  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "ClickHouse database name contains unsupported characters" >&2
    exit 2
  fi
done
if [[ ! "$CLICKHOUSE_BACKUP_DISK" =~ ^[A-Za-z0-9_.-]+$ ]] || [[ ! "$BACKUP_ID" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "backup disk/id contains unsupported characters" >&2
  exit 2
fi

if [[ "$CLICKHOUSE_TARGET_DATABASE" == "$CLICKHOUSE_DATABASE" && "${CONFIRM_RESTORE:-}" != "YES" ]]; then
  echo "set CONFIRM_RESTORE=YES before restoring into the source database name" >&2
  exit 3
fi

args=(--host "$CLICKHOUSE_HOST" --port "$port" --user "$user")
if [[ -n "$config_file" ]]; then
  args+=(--config-file "$config_file")
fi

if [[ "$CLICKHOUSE_TARGET_DATABASE" == "$CLICKHOUSE_DATABASE" ]]; then
  query="RESTORE DATABASE \`$CLICKHOUSE_DATABASE\` FROM Disk('$CLICKHOUSE_BACKUP_DISK', '$BACKUP_ID')"
else
  query="RESTORE DATABASE \`$CLICKHOUSE_DATABASE\` AS \`$CLICKHOUSE_TARGET_DATABASE\` FROM Disk('$CLICKHOUSE_BACKUP_DISK', '$BACKUP_ID')"
fi
clickhouse-client "${args[@]}" --query "$query"
echo "clickhouse_restore_ok source=$CLICKHOUSE_DATABASE target=$CLICKHOUSE_TARGET_DATABASE id=$BACKUP_ID"
