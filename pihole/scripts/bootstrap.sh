#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Pi-hole bootstrap
# -----------------------------------------------------------------------------
# One-time prerequisite for the Pi-hole stack: creates the `macvlan_pihole`
# Docker network that docker-compose.yml references as `external: true`,
# then sets up a macvlan shim so the NAS host (and all Docker containers)
# can reach Pi-hole's LAN IP for DNS.
#
# Reads parameters from a sibling .env if present, else from process env.
# Required keys: PIHOLE_LAN_SUBNET, PIHOLE_LAN_GATEWAY, PIHOLE_PARENT_IFACE,
#                PIHOLE_IP, PIHOLE_SHIM_IP.
#
# PIHOLE_SHIM_IP: any unused IP on your LAN subnet (NOT PIHOLE_IP, NOT the
#                 gateway). Used only as the shim interface address so the
#                 host has a local leg on the macvlan. e.g. 192.168.1.253
#
# Idempotent: safe to re-run; an existing network and shim are left alone.
#
# Persistence: installs autorun.sh onto the DOM config partition, which runs
# pihole-watchdog.sh once per boot to re-assert the shim, its address, the host
# route and /etc/resolv.conf.
#
# Note that QTS executes exactly one user startup script — /tmp/config/autorun.sh
# from the DOM — and only when "Run user defined processes during startup" is
# ticked in Control Panel. An /etc/config/autorun.d/ drop is never executed by
# anything on this NAS.
#
# Must run as root.
#
# Usage:
#   sudo ./bootstrap.sh
# -----------------------------------------------------------------------------
set -euo pipefail

NETWORK_NAME="macvlan_pihole"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/../.env" ]]; then
  # shellcheck disable=SC1091
  set -a; . "${SCRIPT_DIR}/../.env"; set +a
fi

: "${PIHOLE_LAN_SUBNET:?PIHOLE_LAN_SUBNET must be set (see .env.example)}"
: "${PIHOLE_LAN_GATEWAY:?PIHOLE_LAN_GATEWAY must be set (see .env.example)}"
: "${PIHOLE_PARENT_IFACE:?PIHOLE_PARENT_IFACE must be set (see .env.example)}"
: "${PIHOLE_IP:?PIHOLE_IP must be set (see .env.example)}"
: "${PIHOLE_SHIM_IP:?PIHOLE_SHIM_IP must be set (see .env.example)}"

# -----------------------------------------------------------------------------
# 1. macvlan Docker network
# -----------------------------------------------------------------------------
if docker network inspect "${NETWORK_NAME}" >/dev/null 2>&1; then
  echo "Network '${NETWORK_NAME}' already exists — skipping."
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

# -----------------------------------------------------------------------------
# 2. Macvlan shim — lets the host reach PIHOLE_IP directly.
#    Without this, the host kernel drops traffic to its own macvlan children.
#    Delegated to the watchdog so the shim logic lives in exactly one place.
# -----------------------------------------------------------------------------
WATCHDOG_SCRIPT="${SCRIPT_DIR}/pihole-watchdog.sh"
for f in "${WATCHDOG_SCRIPT}" "${SCRIPT_DIR}/pihole-diag.sh" \
         "${SCRIPT_DIR}/install-autorun.sh"; do
  if [[ ! -f "${f}" ]]; then
    echo "Error: required script not found: ${f}" >&2
    exit 1
  fi
  chmod +x "${f}"
done

"${WATCHDOG_SCRIPT}"
echo "Ran ${WATCHDOG_SCRIPT} (shim_ip=${PIHOLE_SHIM_IP}, route -> ${PIHOLE_IP})."

# -----------------------------------------------------------------------------
# 3. Boot persistence via QTS autorun
#    One mechanism, run once per boot. There is deliberately no periodic timer:
#    the shim has been observed surviving 56 days of uptime untouched, so
#    polling would be insuring against a failure that has not been seen, and if
#    autorun does fail the fallback is simply running this script by hand.
# -----------------------------------------------------------------------------
"${SCRIPT_DIR}/install-autorun.sh"
