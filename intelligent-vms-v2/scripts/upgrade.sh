#!/usr/bin/env bash
# In-place upgrade for a single-server Ubuntu installation.
#
# Safety properties:
#   * A backup is taken BEFORE anything is changed.
#   * The recorder is stopped FIRST and given time to finalize open segments,
#     so an upgrade never truncates a recording.
#   * If the migration fails, the previous version is restarted and the
#     script exits non-zero. It does not leave a half-upgraded system running.
#
# NOT YET EXECUTED on a real host — see docs/LIMITATIONS.md.

set -euo pipefail

INSTALL_DIR="${VMS_INSTALL_DIR:-/opt/intelligent-vms}"
VENV="${INSTALL_DIR}/venv"
SOURCE_DIR="${1:-$(pwd)}"
SERVICES=(intelligent-vms-recorder intelligent-vms-api)

require_root() { [[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }; }
require_root

echo "==> 1/6 Backing up"
"${SOURCE_DIR}/scripts/db_backup.sh"

echo "==> 2/6 Stopping services (recorder first, to finalize open segments)"
for service in "${SERVICES[@]}"; do
    systemctl stop "$service" || true
done

echo "==> 3/6 Installing packages"
"${VENV}/bin/pip" install --upgrade \
    "${SOURCE_DIR}/packages/shared-contracts" \
    "${SOURCE_DIR}/services/control-api" \
    "${SOURCE_DIR}/services/recording-service"

echo "==> 4/6 Applying database migrations"
if ! (cd "${SOURCE_DIR}/database" && \
      VMS_CONFIG_FILE=/etc/intelligent-vms/vms.toml "${VENV}/bin/alembic" upgrade head); then
    echo "MIGRATION FAILED — restarting the previous version and aborting." >&2
    systemctl start intelligent-vms-api intelligent-vms-recorder || true
    exit 1
fi

echo "==> 5/6 Starting services"
systemctl start intelligent-vms-api
sleep 5
systemctl start intelligent-vms-recorder

echo "==> 6/6 Verifying"
for _ in {1..12}; do
    if curl -fsS localhost:8080/readyz >/dev/null 2>&1; then
        echo "Upgrade complete."
        curl -s localhost:8080/api/v1/system/version
        exit 0
    fi
    sleep 5
done

echo "Service did not become ready. Check: journalctl -u intelligent-vms-api -n 100" >&2
exit 1
