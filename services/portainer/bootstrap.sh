#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Portainer bootstrap
# -----------------------------------------------------------------------------
# One-time prerequisite for the Portainer stack: creates the named Docker
# volume that docker-compose.yml references as `external: true`.
#
# Run this once on a fresh host (or after a full Docker reset) BEFORE the
# first `docker compose up -d`. Existing volume is left untouched — the
# script is idempotent and safe to re-run.
#
# Usage:
#   ./bootstrap.sh
# -----------------------------------------------------------------------------

set -euo pipefail

VOLUME_NAME="portainer_data"

if docker volume inspect "${VOLUME_NAME}" >/dev/null 2>&1; then
  echo "Volume '${VOLUME_NAME}' already exists — nothing to do."
else
  docker volume create "${VOLUME_NAME}" >/dev/null
  echo "Created volume '${VOLUME_NAME}'."
fi
