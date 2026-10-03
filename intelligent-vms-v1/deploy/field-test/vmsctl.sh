#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
ENV_FILE="${VMS_ENV_FILE:-$ROOT/.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
env_value(){ local key="$1"; python3 - "$ENV_FILE" "$key" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1]); key=sys.argv[2]
for raw in path.read_text(encoding="utf-8").splitlines():
    line=raw.strip()
    if line and not line.startswith("#") and "=" in line:
        k,v=line.split("=",1)
        if k.strip()==key: print(v.strip()); raise SystemExit(0)
raise SystemExit(2)
PY
}
require_env(){
  [[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE; run generate_env.py first" >&2; exit 2; }
  local mode; mode="$(stat -c '%a' "$ENV_FILE")"
  (( (8#$mode & 077) == 0 )) || { echo "$ENV_FILE must not be group/world-readable (expected 600)" >&2; exit 2; }
}
preflight(){
  [[ -r /etc/os-release ]] || { echo "cannot identify operating system" >&2; exit 2; }
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || { echo "field-test baseline requires Ubuntu" >&2; exit 2; }
  [[ "${VERSION_ID:-}" == "22.04" || "${VERSION_ID:-}" == "24.04" ]] || { echo "validated field-test versions are Ubuntu 22.04 or 24.04" >&2; exit 2; }
  case "$(uname -m)" in x86_64|amd64) ;; *) echo "automated Ubuntu field-test baseline is x86_64 only" >&2; exit 2;; esac
  for cmd in docker python3 openssl curl git stat; do command -v "$cmd" >/dev/null || { echo "missing required command: $cmd" >&2; exit 2; }; done
  docker compose version >/dev/null; docker info >/dev/null; require_env
  [[ "$(env_value AUTO_CREATE_SCHEMA)" == "false" ]]
  [[ "$(env_value AUTH_DISABLED)" == "false" ]]
  [[ "$(env_value AUTH_BROWSER_SESSION_ENABLED)" == "true" ]]
  local recordings; recordings="$(env_value VMS_RECORDING_VOLUME)"
  [[ "$recordings" = /* && "$recordings" != "/" ]] || { echo "VMS_RECORDING_VOLUME must be an absolute non-root host directory" >&2; exit 2; }
  [[ -d "$recordings" && -w "$recordings" ]] || { echo "recording directory is missing or not writable: $recordings" >&2; exit 2; }
  "${COMPOSE[@]}" config -q
  echo "field_test_preflight_ok ubuntu=$VERSION_ID arch=$(uname -m) recordings=$recordings"
}
infra_up(){ "${COMPOSE[@]}" up -d --wait postgres redpanda clickhouse; }
migrate(){ preflight; infra_up; "${COMPOSE[@]}" run --rm --no-deps control-api alembic upgrade head; echo "field_test_migration_ok"; }
health(){
  require_env
  curl -fsS http://127.0.0.1:8000/api/v1/system/healthz/ready >/dev/null
  curl -fsS http://127.0.0.1:8000/api/v1/system/health | python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("status")=="ok" and data.get("media_node")=="ok"'
  curl -fsS http://127.0.0.1:8080/ >/dev/null
  curl -fsS http://127.0.0.1:9998/metrics >/dev/null
  echo "field_test_health_ok"
}
start(){ migrate; "${COMPOSE[@]}" up -d --wait control-api; "${COMPOSE[@]}" up -d --wait; health; echo "VMS ready at http://localhost:8080"; }
install_vms(){ preflight; "${COMPOSE[@]}" build; start; }
stop(){ require_env; "${COMPOSE[@]}" stop; echo "field_test_stopped_data_preserved"; }
restart_vms(){ require_env; "${COMPOSE[@]}" restart; "${COMPOSE[@]}" up -d --wait; health; echo "field_test_restart_ok"; }
status(){ require_env; "${COMPOSE[@]}" ps; health; }
diagnostics(){ require_env; python3 tools/field_test_diagnostics.py --env-file "$ENV_FILE"; }
backup(){ require_env; deploy/field-test/field-test-backup.sh; }
restore_postgres(){ require_env; [[ $# -eq 1 ]] || { echo "usage: vmsctl.sh restore-postgres <database.dump>" >&2; exit 2; }; BACKUP_FILE="$1" deploy/field-test/field-test-restore-postgres.sh; }
upgrade(){ preflight; deploy/field-test/field-test-backup.sh; "${COMPOSE[@]}" build; migrate; "${COMPOSE[@]}" up -d --wait; health; echo "field_test_upgrade_ok; rollback requires schema compatibility or database restore"; }
uninstall_vms(){ require_env; "${COMPOSE[@]}" down --remove-orphans; echo "containers/network removed; database volumes, recording directory, .env and backups preserved"; }
purge(){
  require_env
  [[ "${VMS_CONFIRM_PURGE:-}" == "DELETE_FIELD_TEST_DATABASE_VOLUMES" ]] || { echo "refusing purge; set VMS_CONFIRM_PURGE=DELETE_FIELD_TEST_DATABASE_VOLUMES" >&2; exit 3; }
  "${COMPOSE[@]}" down -v --remove-orphans
  echo "named database/state volumes deleted; recording host directory and .env were NOT deleted"
}
case "${1:-}" in
 preflight) preflight;; install) install_vms;; migrate) migrate;; start) start;; stop) stop;; restart) restart_vms;; status|health) status;;
 diagnostics) diagnostics;; backup) backup;; restore-postgres) shift; restore_postgres "$@";; upgrade) upgrade;; uninstall) uninstall_vms;; purge) purge;;
 *) echo "usage: $0 {preflight|install|migrate|start|stop|restart|status|diagnostics|backup|restore-postgres|upgrade|uninstall|purge}" >&2; exit 2;;
esac
