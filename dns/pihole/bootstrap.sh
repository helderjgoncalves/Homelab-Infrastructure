#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Pi-hole bootstrap
# -----------------------------------------------------------------------------
# One-time prerequisite for the Pi-hole stack: creates the `macvlan_pihole`
# Docker network that docker-compose.yml references as `external: true`.
#
# Reads parameters from a sibling .env if present, else from process env.
# Required keys: PIHOLE_LAN_SUBNET, PIHOLE_LAN_GATEWAY, PIHOLE_PARENT_IFACE.
#
# Idempotent: safe to re-run; existing network is left untouched.
#
# Usage:
#   ./bootstrap.sh
# -----------------------------------------------------------------------------

set -euo pipefail

NETWORK_NAME="macvlan_pihole"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; . "${SCRIPT_DIR}/.env"; set +a
fi

: "${PIHOLE_LAN_SUBNET:?PIHOLE_LAN_SUBNET must be set (see .env.example)}"
: "${PIHOLE_LAN_GATEWAY:?PIHOLE_LAN_GATEWAY must be set (see .env.example)}"
: "${PIHOLE_PARENT_IFACE:?PIHOLE_PARENT_IFACE must be set (see .env.example)}"

if docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  echo "Network '${NETWORK_NAME}' already exists — nothing to do."
else
  docker network create -d macvlan \
    --subnet="${PIHOLE_LAN_SUBNET}" \
    --gateway="${PIHOLE_LAN_GATEWAY}" \
    -o parent="${PIHOLE_PARENT_IFACE}" \
    "${NETWORK_NAME}" >/dev/null
  echo "Created macvlan network '${NETWORK_NAME}'" \
       "(subnet=${PIHOLE_LAN_SUBNET}, gw=${PIHOLE_LAN_GATEWAY}," \
       "parent=${PIHOLE_PARENT_IFACE})."
fi
