#!/usr/bin/env bash
# PostgreSQL backup for Intelligent VMS.
#
# IMPORTANT: a database backup alone is NOT sufficient to restore the system.
# Camera passwords are encrypted with the master key, which deliberately lives
# OUTSIDE the database. Without keys/vms-master.key the restored system will
# not be able to authenticate to any camera.
#
# Back up BOTH. See docs/operations/disaster-recovery.md.

set -euo pipefail

BACKUP_DIR="${VMS_BACKUP_DIR:-/var/backups/intelligent-vms}"
DATA_ROOT="${VMS_DATA_ROOT:-/var/lib/intelligent-vms}"
DB_NAME="${VMS_DB_NAME:-intelligentvms}"
DB_USER="${VMS_DB_USER:-vms}"
DB_HOST="${VMS_DB_HOST:-localhost}"
RETAIN_DAYS="${VMS_BACKUP_RETAIN_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "==> Dumping database ${DB_NAME}"
pg_dump --host="$DB_HOST" --username="$DB_USER" --dbname="$DB_NAME" \
        --format=custom --compress=9 \
        --file="${BACKUP_DIR}/db-${STAMP}.dump"

echo "==> Copying the master key (REQUIRED to decrypt camera credentials)"
if [[ -f "${DATA_ROOT}/keys/vms-master.key" ]]; then
    install -m 0600 "${DATA_ROOT}/keys/vms-master.key" \
            "${BACKUP_DIR}/vms-master-${STAMP}.key"
else
    echo "WARNING: no master key found at ${DATA_ROOT}/keys/vms-master.key" >&2
fi

echo "==> Copying configuration"
tar -czf "${BACKUP_DIR}/config-${STAMP}.tar.gz" -C /etc intelligent-vms 2>/dev/null \
    || echo "WARNING: /etc/intelligent-vms not found" >&2

echo "==> Pruning backups older than ${RETAIN_DAYS} days"
find "$BACKUP_DIR" -type f -mtime "+${RETAIN_DAYS}" -delete

cat <<SUMMARY

Backup complete: ${BACKUP_DIR}
  db-${STAMP}.dump
  vms-master-${STAMP}.key    <-- store this separately and securely
  config-${STAMP}.tar.gz

Recorded video is NOT included. It lives on the storage volumes and is
normally protected by RAID and retention policy rather than by backup.
SUMMARY
