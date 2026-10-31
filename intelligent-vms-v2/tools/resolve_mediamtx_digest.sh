#!/usr/bin/env sh
# Resolve the immutable digest of an official MediaMTX image tag so it can be
# pinned in deploy/docker/docker-compose.yml. Run where the registry is
# reachable. Prints the multi-arch index digest; never guesses.
set -eu
TAG="${1:?usage: resolve_mediamtx_digest.sh <version, e.g. 1.21.2>}"
IMAGE="docker.io/bluenviron/mediamtx:${TAG}"
if command -v docker >/dev/null 2>&1; then
  DIGEST="$(docker buildx imagetools inspect "$IMAGE" --format '{{json .Manifest.Digest}}' | tr -d '"')"
elif command -v crane >/dev/null 2>&1; then
  DIGEST="$(crane digest "$IMAGE")"
else
  echo "need docker buildx or crane" >&2; exit 2
fi
case "$DIGEST" in sha256:*) ;; *) echo "unexpected digest output: $DIGEST" >&2; exit 3;; esac
echo "bluenviron/mediamtx:${TAG}@${DIGEST}"
