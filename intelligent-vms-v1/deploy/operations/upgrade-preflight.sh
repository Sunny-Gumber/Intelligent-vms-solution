#!/usr/bin/env bash
set -euo pipefail

: "${CHART_PATH:?CHART_PATH is required}"
: "${VALUES_FILE:?VALUES_FILE is required}"

release="${RELEASE_NAME:-vms}"
namespace="${NAMESPACE:-vms}"
render_dir="${RENDER_DIR:-/tmp/intelligent-vms-upgrade-preflight}"

rm -rf "$render_dir"
mkdir -p "$render_dir"

helm lint "$CHART_PATH" -f "$VALUES_FILE"
helm template "$release" "$CHART_PATH"   --namespace "$namespace"   -f "$VALUES_FILE"   > "$render_dir/rendered.yaml"

grep -q 'kind: Deployment' "$render_dir/rendered.yaml"
grep -q '/api/v1/system/healthz/ready' "$render_dir/rendered.yaml"

echo "upgrade_preflight_ok rendered=$render_dir/rendered.yaml"
