#!/usr/bin/env bash
set -euo pipefail

: "${BACKUP_DIR:?BACKUP_DIR is required}"

node_state="${REGIONAL_NODE_STATE_PATH:-/var/lib/vms-node/fence-state.json}"
spool_db="${REGIONAL_SPOOL_DB_PATH:-/var/lib/vms-spool/spool.db}"
backup_id="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"

if [[ "$node_state" == *"'"* || "$spool_db" == *"'"* || "$BACKUP_DIR" == *"'"* || "$backup_id" == *"'"* ]]; then
  echo "regional state and backup paths must not contain single quotes" >&2
  exit 2
fi

umask 077
target="${BACKUP_DIR%/}/${backup_id}/regional"
mkdir -p "$target"

if [[ -f "$node_state" ]]; then
  cp --preserve=mode,timestamps "$node_state" "$target/fence-state.json"
fi

if [[ -f "$spool_db" ]]; then
  sqlite3 "$spool_db" ".backup '$target/spool.db'"
fi

if [[ ! -f "$target/fence-state.json" && ! -f "$target/spool.db" ]]; then
  echo "no regional state files were found" >&2
  exit 4
fi

(
  cd "$target"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\0'     | sort -z     | xargs -0 -r sha256sum > SHA256SUMS
)

echo "regional_state_backup_ok id=$backup_id path=$target"
