#!/usr/bin/env bash
# Build + tag + (optionally) push the hyperagent0 image.
#
# Usage:
#   ./docker/hyperagent0/build.sh                       # build :dev
#   TAG=0.1.0 ./docker/hyperagent0/build.sh             # build :0.1.0 + :latest
#   PUSH=1 TAG=0.1.0 ./docker/hyperagent0/build.sh      # build + push to Docker Hub
#
# Assumes `docker login` has been done already.

set -euo pipefail

REPO="${REPO:-bayeslearner/hyperagent0}"
TAG="${TAG:-dev}"
PUSH="${PUSH:-0}"

cd "$(dirname "$0")/../.."

echo "==> Building ${REPO}:${TAG}"
docker build \
    -f docker/hyperagent0/Dockerfile \
    -t "${REPO}:${TAG}" \
    .

if [ "${TAG}" != "dev" ]; then
    echo "==> Also tagging as :latest"
    docker tag "${REPO}:${TAG}" "${REPO}:latest"
fi

if [ "${PUSH}" = "1" ]; then
    echo "==> Pushing ${REPO}:${TAG}"
    docker push "${REPO}:${TAG}"
    if [ "${TAG}" != "dev" ]; then
        docker push "${REPO}:latest"
    fi
fi

echo "==> Done."
docker image ls "${REPO}" --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
