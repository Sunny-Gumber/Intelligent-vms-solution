#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_PATH:?BACKUP_PATH is required}"

if [[ "${CONFIRM_RESTORE:-}" != "YES" ]]; then
  echo "set CONFIRM_RESTORE=YES after stopping node-agent and regional-spool workloads" >&2
  exit 3
fi

node_state="${REGIONAL_NODE_STATE_PATH:-/var/lib/vms-node/fence-state.json}"
spool_db="${REGIONAL_SPOOL_DB_PATH:-/var/lib/vms-spool/spool.db}"

if [[ "$node_state" == *"'"* || "$spool_db" == *"'"* || "$BACKUP_PATH" == *"'"* ]]; then
  echo "regional state and backup paths must not contain single quotes" >&2
  exit 2
fi

if [[ ! -d "$BACKUP_PATH" ]]; then
  echo "BACKUP_PATH must be a regional backup directory" >&2
  exit 2
fi

if [[ -f "$BACKUP_PATH/SHA256SUMS" ]]; then
  (
    cd "$BACKUP_PATH"
    sha256sum -c SHA256SUMS
  )
fi

if [[ -f "$BACKUP_PATH/fence-state.json" ]]; then
  mkdir -p "$(dirname "$node_state")"
  tmp="${node_state}.restore.tmp"
  cp "$BACKUP_PATH/fence-state.json" "$tmp"
  mv "$tmp" "$node_state"
fi

if [[ -f "$BACKUP_PATH/spool.db" ]]; then
  mkdir -p "$(dirname "$spool_db")"
  sqlite3 "$spool_db" ".restore '$BACKUP_PATH/spool.db'"
fi

echo "regional_state_restore_ok"
