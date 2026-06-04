#!/bin/sh
# Resolve script dir so we can be invoked from anywhere (cwd may be /).
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

LOG=${SCRIPT_DIR}/post-pihole.log
SCRIPT=${SCRIPT_DIR}/post-pihole.sh

if [ -f "${SCRIPT_DIR}/../.env" ]; then
  set -a
  . "${SCRIPT_DIR}/../.env"
  set +a
fi

# Directory containing the docker CLI. Must be set in .env — no default,
# as the path is host-specific.
if [ -z "${CS_DOCKER_BIN:-}" ]; then
  echo "[$(date)] ERROR: CS_DOCKER_BIN is not set (check ${SCRIPT_DIR}/../.env)" >> "$LOG"
  exit 1
fi
PATH=${CS_DOCKER_BIN}:${PATH}
export PATH

echo "[$(date)] waiting for pi-hole..." > "$LOG"

# Wait for the Docker daemon to be ready
until docker ps >/dev/null 2>&1; do
  echo "[$(date)] waiting for Docker daemon..." >> "$LOG"
  sleep 5
done

# Wait until the pihole container reports healthy
# (works if your container has a healthcheck; otherwise see note below)
until [ "$(docker inspect -f '{{.State.Health.Status}}' pihole 2>/dev/null)" = "healthy" ]; do
  echo "[$(date)] waiting for pi-hole to be healthy..." >> "$LOG"
  sleep 5
done

echo "[$(date)] pi-hole healthy, running script" >> "$LOG"
"$SCRIPT" >> "$LOG" 2>&1
echo "[$(date)] done" >> "$LOG"