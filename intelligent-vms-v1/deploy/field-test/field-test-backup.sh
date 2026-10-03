#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
ENV_FILE="${VMS_ENV_FILE:-$ROOT/.env}"; BACKUP_ROOT="${BACKUP_DIR:-$ROOT/field-test-backups}"; BACKUP_ID="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"; TARGET="$BACKUP_ROOT/$BACKUP_ID"
COMPOSE=(docker compose --env-file "$ENV_FILE")
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 2; }
umask 077; mkdir -p "$TARGET/postgres" "$TARGET/config"
tmp="$TARGET/postgres/database.dump.tmp"
"${COMPOSE[@]}" exec -T postgres pg_dump -U vms -d vms --format=custom --no-owner --no-privileges > "$tmp"
mv "$tmp" "$TARGET/postgres/database.dump"
(cd "$TARGET/postgres" && sha256sum database.dump > database.dump.sha256)
cp compose.yaml "$TARGET/config/compose.yaml"
git rev-parse HEAD > "$TARGET/config/repository-commit.txt" 2>/dev/null || true
recording_path="$(python3 - "$ENV_FILE" <<'PY'
from pathlib import Path
import sys
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.startswith("VMS_RECORDING_VOLUME="): print(line.split("=",1)[1]); break
PY
)"
printf 'Recording media is NOT included in this backup. Host recording path: %s\n' "$recording_path" > "$TARGET/RECORDING_MEDIA_NOT_BACKED_UP.txt"
printf 'ClickHouse event/search history is NOT included in this quick field-test backup. Use Phase 9 native ClickHouse backup when required.\n' > "$TARGET/CLICKHOUSE_NOT_BACKED_UP.txt"
if [[ "${INCLUDE_SECRETS:-NO}" == "YES" ]]; then cp "$ENV_FILE" "$TARGET/config/.env"; chmod 600 "$TARGET/config/.env"; else printf 'Runtime .env/secrets excluded. Preserve the original VMS_SECRET_KEY separately.\n' > "$TARGET/config/SECRETS_NOT_BACKED_UP.txt"; fi
python3 tools/phase9_dr_evidence.py manifest --root "$TARGET" --output "$TARGET.manifest.json"
chmod -R go-rwx "$TARGET" "$TARGET.manifest.json"
echo "field_test_backup_ok path=$TARGET"
