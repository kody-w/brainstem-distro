#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
REMOTE_DIR=/share/CACHEDEV1_DATA/rapp-lab/keeper
CONTAINER="keeper-e2e-$(date +%s)-$$"
SSH=(ssh -o BatchMode=yes -J rappterone admin1@192.168.1.209)

COPYFILE_DISABLE=1 tar --no-xattrs -C "$ROOT" -cf - \
  keeper/keeper.py keeper/tests/e2e_linux.sh |
  "${SSH[@]}" "mkdir -p '$REMOTE_DIR' && tar -xf - -C '$REMOTE_DIR'"

"${SSH[@]}" bash -s -- "$REMOTE_DIR" "$CONTAINER" <<'REMOTE'
set -euo pipefail
REMOTE_DIR=$1
CONTAINER=$2
export DOCKER_HOST=unix:///var/run/system-docker.sock
export PATH=/share/CACHEDEV1_DATA/.qpkg/container-station/bin:$PATH

docker run -d \
  --name "$CONTAINER" \
  -v "$REMOTE_DIR:/work:ro" \
  debian:12 \
  bash /work/keeper/tests/e2e_linux.sh >/dev/null

echo "NAS_CONTAINER $CONTAINER"
set +e
docker logs --follow "$CONTAINER"
LOG_STATUS=$?
set -e
EXIT_CODE=$(docker inspect --format '{{.State.ExitCode}}' "$CONTAINER")
if [ "$EXIT_CODE" -eq 0 ]; then
  docker rm "$CONTAINER" >/dev/null
else
  echo "NAS_CONTAINER_RETAINED $CONTAINER" >&2
fi
if [ "$LOG_STATUS" -ne 0 ]; then
  exit "$LOG_STATUS"
fi
exit "$EXIT_CODE"
REMOTE
