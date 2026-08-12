#!/bin/sh
# -----------------------------------------------------------------------------
# Pi-hole DNS diagnostic snapshot
# -----------------------------------------------------------------------------
# Dumps everything that determines whether the NAS and its containers can
# resolve names, to pihole-diag.log. Read-only: it changes nothing.
#
# Called by pihole-watchdog.sh on its first run after a boot — once in --fast
# mode before repairing, once in full mode after. The fast pass deliberately
# skips every probe that can block: those probes run against a broken resolver
# and each one that times out is time the NAS spends without DNS.
#
# Usage: ./pihole-diag.sh [TAG] [--fast]
# -----------------------------------------------------------------------------
set -u

TAG=${1:-MANUAL}
FAST=${2:-}
SHIM_IFACE="macvlan_shim0"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LOG="${SCRIPT_DIR}/pihole-diag.log"
LOG_MAX_LINES=2000
PIHOLE_HEALTH_WAIT=90   # seconds to let the container finish starting

if [ -f "${SCRIPT_DIR}/../.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/../.env"
  set +a
fi

# No fallback values on purpose. Defaulting would keep this NAS's addressing in
# a tracked file, and — worse — would let a missing .env produce a confident
# report about the wrong addresses. Fail the same way pihole-watchdog.sh does.
for var in PIHOLE_IP PIHOLE_SHIM_IP PIHOLE_PARENT_IFACE; do
  eval "value=\${${var}:-}"
  if [ -z "$value" ]; then
    echo "ERROR: ${var} is not set (check ${SCRIPT_DIR}/../.env)" >&2
    echo "[$(date)] ERROR: ${var} is not set — no snapshot taken" >> "$LOG"
    exit 1
  fi
done
[ -n "${CS_DOCKER_BIN:-}" ] && PATH="${CS_DOCKER_BIN}:${PATH}" && export PATH

run() { if command -v timeout >/dev/null 2>&1; then timeout 5 "$@"; else "$@"; fi; }
out() { echo "$@" >> "$LOG"; }
section() { out ""; out "-- $* --"; }
now_s() { cut -d. -f1 /proc/uptime 2>/dev/null || echo 0; }

# The host-side repair finishes long before the Pi-hole container is ready to
# answer, so probing immediately records timeouts that read like a failed boot
# when they only mean "checked too early". Wait for it first — this runs after
# the repair, in the background, so waiting costs nothing. Bounded on wall
# clock rather than iterations, so a hanging docker CLI cannot stretch it.
wait_for_pihole() {
  run docker ps >/dev/null 2>&1 || return 0   # daemon down; section 7 reports that
  started=$(now_s)
  deadline=$((started + PIHOLE_HEALTH_WAIT))
  while [ "$(now_s)" -lt "$deadline" ]; do
    case "$(run docker inspect -f '{{.State.Health.Status}}' pihole 2>/dev/null)" in
      healthy)
        waited=$(( $(now_s) - started ))
        [ "$waited" -gt 1 ] && out "(waited ${waited}s for pihole to report healthy)"
        return 0
        ;;
    esac
    sleep 2
  done
  out "(pihole still not healthy after ${PIHOLE_HEALTH_WAIT}s — timeouts below are that, not a broken shim)"
  return 1
}

UPTIME_S=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo "?")

out "============================================================"
out "[$(date)] ${TAG}${FAST:+ (fast)}  (uptime ${UPTIME_S}s, $((UPTIME_S / 60)) min since boot)"
out "============================================================"

# --- State: instant, no network I/O -----------------------------------------
section "1. shim interface (${SHIM_IFACE})"
if ip link show "$SHIM_IFACE" >/dev/null 2>&1; then
  ip addr show "$SHIM_IFACE" >> "$LOG" 2>&1
else
  out "ABSENT — nothing created it since boot"
fi

section "2. host route to Pi-hole (${PIHOLE_IP})"
r=$(ip route show "${PIHOLE_IP}/32" 2>/dev/null)
[ -n "$r" ] && out "$r" || out "NO /32 ROUTE — host will try to reach ${PIHOLE_IP} over ${PIHOLE_PARENT_IFACE}, which macvlan isolation blocks"
out "ip route get: $(ip route get "${PIHOLE_IP}" 2>&1 | head -1)"

section "3. /etc/resolv.conf"
out "mtime: $(ls -l /etc/resolv.conf 2>/dev/null | awk '{print $6, $7, $8}')"
sed 's/^/  /' /etc/resolv.conf >> "$LOG" 2>&1
out "(127.0.1.1 = QTS's own dnsmasq, which binds upstreams to ${PIHOLE_PARENT_IFACE} and so cannot reach Pi-hole)"

section "4. QTS resolver config"
out "dnsmasq upstreams: $(cat /etc/resolv.dnsmasq 2>/dev/null | tr '\n' ' ')"
out "DHCP lease says:   $(grep 'domain-name-servers' /etc/config/dhclient/eth0.leases 2>/dev/null | tail -1 | sed 's/^ *//')"

section "5. parent interface"
ip -br addr show "$PIHOLE_PARENT_IFACE" >> "$LOG" 2>&1
out "default route: $(ip route show default 2>/dev/null | head -1)"

# --- Probes: can block, so skipped in fast mode ------------------------------
if [ "$FAST" = "--fast" ]; then
  out ""
  out "-- probes skipped (fast pass, DNS still broken at this point) --"
else
  wait_for_pihole
  section "6. can we reach and query Pi-hole?"
  out "resolve via ${PIHOLE_IP}: $(run nslookup google.com "$PIHOLE_IP" 2>&1 | tail -3 | tr '\n' ' ')"
  out "resolve via system resolver: $(run nslookup google.com 2>&1 | tail -3 | tr '\n' ' ')"
  out "blocked domain via ${PIHOLE_IP}: $(run nslookup doubleclick.net "$PIHOLE_IP" 2>&1 | grep -c '0\.0\.0\.0') (1 = Pi-hole is filtering)"

  section "7. docker / pihole container"
  if run docker ps >/dev/null 2>&1; then
    out "daemon: up"
    out "dockerd --dns: $(tr '\0' ' ' < /proc/$(pidof dockerd 2>/dev/null | awk '{print $1}')/cmdline 2>/dev/null | grep -o '\-\-dns [0-9.]*' | head -1)"
    out "pihole: $(run docker inspect -f '{{.State.Status}} health={{.State.Health.Status}}' pihole 2>/dev/null | head -1 || echo 'not found')"
    section "7b. container resolv.conf (set at container start from dockerd --dns)"
    for c in npm uptime-kuma ntfy immich_server qbittorrent dockge; do
      out "  ${c}: $(run docker exec "$c" cat /etc/resolv.conf 2>/dev/null | grep nameserver | tr '\n' ' ')"
    done
  else
    out "daemon: NOT REACHABLE (Container Station not up yet, or no permission)"
  fi
fi

out ""

lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
if [ "$lines" -gt "$LOG_MAX_LINES" ]; then
  tail -n $((LOG_MAX_LINES / 2)) "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
fi

exit 0
