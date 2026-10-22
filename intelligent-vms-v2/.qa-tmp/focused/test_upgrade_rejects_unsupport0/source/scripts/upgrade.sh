#!/usr/bin/env bash
# In-place upgrade for a single-server Ubuntu installation.
#
# Safety properties:
#   * A backup is taken BEFORE anything is changed.
#   * The recorder is stopped FIRST and given time to finalize open segments,
#     so an upgrade never truncates a recording.
#   * If the migration fails, services remain stopped: packages have already
#     been upgraded, so restarting them against an uncertain schema is unsafe.
#
# NOT YET EXECUTED on a real host — see docs/LIMITATIONS.md.

set -euo pipefail

INSTALL_DIR="${VMS_INSTALL_DIR:-/opt/intelligent-vms}"
VENV="${INSTALL_DIR}/venv"
SOURCE_DIR="${1:-$(pwd)}"
SERVICES=(intelligent-vms-recorder intelligent-vms-api)
NATIVE_ENV_FILE=/home/runner/work/Intelligent-vms-solution/Intelligent-vms-solution/intelligent-vms-v2/.qa-tmp/focused/test_upgrade_rejects_unsupport0/vms.env
NATIVE_TOML_FILE=/home/runner/work/Intelligent-vms-solution/Intelligent-vms-solution/intelligent-vms-v2/.qa-tmp/focused/test_upgrade_rejects_unsupport0/vms.toml

require_root() { :; }
require_root

# Configuration precedence is process VMS_* variables, selected vms.env,
# TOML fallback, then built-in defaults. Keep this process environment for
# preflight, backup, and Alembic so all three use the same effective settings.
if [[ -f "$NATIVE_ENV_FILE" ]]; then
    export VMS_ENV_FILE="$NATIVE_ENV_FILE"
elif [[ -f "$NATIVE_TOML_FILE" ]]; then
    export VMS_CONFIG_FILE="$NATIVE_TOML_FILE"
fi

# Load settings from this source tree as well as the installed dependencies.
SOURCE_PYTHONPATH="${SOURCE_DIR}/services/control-api:${SOURCE_DIR}/packages/shared-contracts"
export PYTHONPATH="${SOURCE_PYTHONPATH}${PYTHONPATH:+:${PYTHONPATH}}"

# Alembic otherwise accepts the development SQLite default when configuration
# is missing. Reject it before backup, package installation, or service stops.
if ! "${VENV}/bin/python" -c \
    'from sqlalchemy.engine import make_url; from app.config import get_settings; raise SystemExit(0 if make_url(get_settings().database_url).get_backend_name() == "postgresql" else 1)' \
    >/dev/null 2>&1; then
    echo "Database configuration must resolve to PostgreSQL; refusing upgrade." >&2
    exit 1
fi

echo "==> 1/6 Backing up"
bash "${SOURCE_DIR}/scripts/db_backup.sh"

echo "==> 2/6 Stopping services (recorder first, to finalize open segments)"
for service in "${SERVICES[@]}"; do
    systemctl stop "$service"
done

echo "==> 3/6 Installing packages"
"${VENV}/bin/pip" install --upgrade \
    "${SOURCE_DIR}/packages/shared-contracts" \
    "${SOURCE_DIR}/services/control-api" \
    "${SOURCE_DIR}/services/recording-service"

echo "==> 4/6 Applying database migrations"
if ! (cd "${SOURCE_DIR}/database" && \
      "${VENV}/bin/alembic" upgrade head); then
    echo "MIGRATION FAILED — services remain stopped; upgraded packages were not rolled back." >&2
    echo "Preserve the pre-upgrade backup. Restore compatible packages/schema using docs/operations/upgrade.md before starting services." >&2
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
