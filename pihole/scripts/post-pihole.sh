#!/bin/sh
# -----------------------------------------------------------------------------
# Post-Pi-hole setup
# -----------------------------------------------------------------------------
# Runs *after* the pihole container reports healthy (invoked by wait-pihole.sh).
#
# Responsibilities:
#   1. (Re)create the macvlan shim so the host can reach PIHOLE_IP directly.
#   2. Point the host's /etc/resolv.conf at Pi-hole.
#
# Env vars (sourced by wait-pihole.sh from ../.env):
#   PIHOLE_PARENT_IFACE, PIHOLE_SHIM_IP, PIHOLE_IP
# -----------------------------------------------------------------------------
set -eu

SHIM_IFACE="macvlan_shim0"

: "${PIHOLE_PARENT_IFACE:?PIHOLE_PARENT_IFACE must be set}"
: "${PIHOLE_SHIM_IP:?PIHOLE_SHIM_IP must be set}"
: "${PIHOLE_IP:?PIHOLE_IP must be set}"

if ! ip link show "${SHIM_IFACE}" >/dev/null 2>&1; then
  ip link add "${SHIM_IFACE}" link "${PIHOLE_PARENT_IFACE}" type macvlan mode bridge
  ip addr add "${PIHOLE_SHIM_IP}/32" dev "${SHIM_IFACE}"
  ip link set "${SHIM_IFACE}" up
  ip route add "${PIHOLE_IP}/32" dev "${SHIM_IFACE}"
  echo "Created macvlan shim '${SHIM_IFACE}' (shim_ip=${PIHOLE_SHIM_IP}, route -> ${PIHOLE_IP})."
else
  echo "Shim interface '${SHIM_IFACE}' already exists — skipping."
fi

echo "nameserver ${PIHOLE_IP}" > /etc/resolv.conf
echo "Set /etc/resolv.conf -> ${PIHOLE_IP}."
