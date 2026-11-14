#!/usr/bin/env bash
set -euo pipefail

# Publish media-loader to a registry from the local/LAN machine.
#
# Examples:
#   TAG=0.1.0 ./scripts/publish-image.sh
#   PLATFORMS=linux/amd64,linux/arm64 BUILDER=lan ./scripts/publish-image.sh
#
# BUILDER must be a buildx builder with native nodes for every requested
# platform. For a single arm64 host use PLATFORMS=linux/arm64 and merge the
# amd64 image later with `docker buildx imagetools create` (see README).

REGISTRY="${REGISTRY:-docker.ithub24.ru}"
IMAGE="${IMAGE:-${REGISTRY}/i-panov/media-loader}"
TAG="${TAG:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}"
PLATFORMS="${PLATFORMS:-linux/arm64}"
BUILDER="${BUILDER:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

args=(
  buildx build
  --platform "${PLATFORMS}"
  -t "${IMAGE}:${TAG}"
  --push
  .
)

if [[ -n "${BUILDER}" ]]; then
  args=(
    buildx build
    --builder "${BUILDER}"
    --platform "${PLATFORMS}"
    -t "${IMAGE}:${TAG}"
    --push
    .
  )
fi

echo "publishing ${IMAGE}:${TAG} for ${PLATFORMS}"
docker "${args[@]}"
