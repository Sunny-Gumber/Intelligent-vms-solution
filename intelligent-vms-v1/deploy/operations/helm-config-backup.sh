#!/usr/bin/env bash
set -euo pipefail

: "${RELEASE_NAME:?RELEASE_NAME is required}"
: "${NAMESPACE:?NAMESPACE is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"

backup_id="${BACKUP_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
target="${BACKUP_DIR%/}/${backup_id}/helm"
mkdir -p "$target"
umask 077

helm get values "$RELEASE_NAME" --namespace "$NAMESPACE" --all -o yaml > "$target/values.yaml"
helm get manifest "$RELEASE_NAME" --namespace "$NAMESPACE" > "$target/manifest.yaml"
helm history "$RELEASE_NAME" --namespace "$NAMESPACE" -o json > "$target/history.json"

(
  cd "$target"
  sha256sum values.yaml manifest.yaml history.json > SHA256SUMS
)

echo "helm_config_backup_ok release=$RELEASE_NAME namespace=$NAMESPACE path=$target"
