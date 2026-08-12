#!/bin/sh
# -----------------------------------------------------------------------------
# Pi-hole shim watchdog
# -----------------------------------------------------------------------------
# Re-asserts the host-side state Pi-hole needs, repairing only what is actually
# missing:
#   1. macvlan shim interface exists
#   2. shim carries PIHOLE_SHIM_IP and is up
#   3. host route to PIHOLE_IP goes over the shim
#   4. /etc/resolv.conf points at Pi-hole
#
# Each property is checked independently on purpose. The previous scripts
# guarded all four behind a single "does the interface exist?" test, so an
# interface that survived but lost its address or route (parent flap, netmgr
# restart, Container Station upgrade) was reported healthy and never repaired.
#
# Run once per boot by autorun.sh on the DOM; also safe to run by hand.
# Silent when everything is already correct — only repairs and errors are
# logged, so the log stays small. Exits non-zero if a repair failed, which is
# autorun.sh's signal to retry.
#
# Must run as root (needs `ip` and write access to /etc/resolv.conf).
# -----------------------------------------------------------------------------
set -u

SHIM_IFACE="macvlan_shim0"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
LOG="${SCRIPT_DIR}/pihole-watchdog.log"
LOG_MAX_LINES=500

log() { echo "[$(date)] $*" >> "$LOG"; }

# Keep the log bounded across many boots.
trim_log() {
  [ -f "$LOG" ] || return 0
  lines=$(wc -l < "$LOG" 2>/dev/null) || return 0
  [ "$lines" -le "$LOG_MAX_LINES" ] && return 0
  tail -n $((LOG_MAX_LINES / 2)) "$LOG" > "${LOG}.tmp" 2>/dev/null &&
    mv "${LOG}.tmp" "$LOG"
}

if [ -f "${SCRIPT_DIR}/../.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/../.env"
  set +a
else
  log "ERROR: ${SCRIPT_DIR}/../.env not found — cannot continue."
  trim_log
  exit 1
fi

for var in PIHOLE_PARENT_IFACE PIHOLE_SHIM_IP PIHOLE_IP; do
  eval "value=\${${var}:-}"
  if [ -z "$value" ]; then
    log "ERROR: ${var} is not set (check ${SCRIPT_DIR}/../.env)."
    trim_log
    exit 1
  fi
done

# First run since boot: record the state the machine actually came up in,
# BEFORE repairing anything, then again afterwards. /tmp is tmpfs, so the
# marker file disappears on reboot. This is what turns a reboot into evidence.
# The pre-pass is --fast: its probes would run against a resolver that is still
# broken, and every timeout is time the NAS spends without DNS.
BOOT_MARKER="/tmp/.pihole-watchdog-boot-seen"
DIAG="${SCRIPT_DIR}/pihole-diag.sh"
first_run_since_boot=0
if [ ! -f "$BOOT_MARKER" ]; then
  first_run_since_boot=1
  : > "$BOOT_MARKER" 2>/dev/null
  log "first run since boot (uptime $(cut -d. -f1 /proc/uptime 2>/dev/null)s) — see pihole-diag.log"
  [ -x "$DIAG" ] && "$DIAG" PRE-REPAIR --fast
fi

repaired=0
failed=0   # non-zero exit tells autorun.sh to retry

# 1. Shim interface -----------------------------------------------------------
if ! ip link show "$SHIM_IFACE" >/dev/null 2>&1; then
  if ip link add "$SHIM_IFACE" link "$PIHOLE_PARENT_IFACE" type macvlan \
      mode bridge 2>>"$LOG"; then
    log "created ${SHIM_IFACE} (parent=${PIHOLE_PARENT_IFACE})"
    repaired=1
  else
    # Parent not ready yet (early boot) — autorun.sh retries.
    log "ERROR: failed to create ${SHIM_IFACE} on ${PIHOLE_PARENT_IFACE}"
    trim_log
    exit 1   # nothing else can work; let the caller retry
  fi
fi

# 2. Shim address -------------------------------------------------------------
if ! ip -4 addr show dev "$SHIM_IFACE" 2>/dev/null |
     grep -q "inet ${PIHOLE_SHIM_IP}/32"; then
  if ip addr replace "${PIHOLE_SHIM_IP}/32" dev "$SHIM_IFACE" 2>>"$LOG"; then
    log "assigned ${PIHOLE_SHIM_IP}/32 to ${SHIM_IFACE}"
    repaired=1
  else
    log "ERROR: failed to assign ${PIHOLE_SHIM_IP}/32 to ${SHIM_IFACE}"
    failed=1
  fi
fi

# 3. Link state ---------------------------------------------------------------
case "$(ip link show "$SHIM_IFACE" 2>/dev/null)" in
  *"state UP"*) ;;
  *)
    if ip link set "$SHIM_IFACE" up 2>>"$LOG"; then
      log "brought ${SHIM_IFACE} up"
      repaired=1
    else
      log "ERROR: failed to bring ${SHIM_IFACE} up"
      failed=1
    fi
    ;;
esac

# 4. Host route to Pi-hole ----------------------------------------------------
if ! ip route show "${PIHOLE_IP}/32" 2>/dev/null |
     grep -q "dev ${SHIM_IFACE}"; then
  if ip route replace "${PIHOLE_IP}/32" dev "$SHIM_IFACE" 2>>"$LOG"; then
    log "set route ${PIHOLE_IP}/32 -> ${SHIM_IFACE}"
    repaired=1
  else
    log "ERROR: failed to set route ${PIHOLE_IP}/32 -> ${SHIM_IFACE}"
    failed=1
  fi
fi

# 5. Resolver -----------------------------------------------------------------
# This is the step that makes the NAS itself able to resolve, and it is not
# optional. QTS points /etc/resolv.conf at 127.0.1.1, its own dnsmasq, and
# /etc sits on the ramdisk root so that is restored on every boot. That dnsmasq
# binds each upstream to the interface the DNS is configured on — its log shows
# "using nameserver 192.168.1.2#53(via eth0)" — and eth0 cannot reach Pi-hole,
# because the kernel drops traffic from a parent interface to its own macvlan
# child. Every query it forwards therefore goes to the router instead.
#
# So the point of writing this file is to bypass QTS's dnsmasq entirely and
# talk to Pi-hole directly over the shim. Setting the DNS server in Control
# Panel does NOT achieve this: that only feeds dnsmasq's upstream list.
#
# Written only when it differs, so the mtime still shows when something else
# last clobbered it.
RESOLV_WANT="nameserver ${PIHOLE_IP}"
RESOLV_WAS=$(cat /etc/resolv.conf 2>/dev/null | tr '\n' ' ')
if [ "$(cat /etc/resolv.conf 2>/dev/null)" != "$RESOLV_WANT" ]; then
  if echo "$RESOLV_WANT" > /etc/resolv.conf 2>>"$LOG"; then
    log "rewrote /etc/resolv.conf -> ${PIHOLE_IP} (was: ${RESOLV_WAS})"
    repaired=1
  else
    log "ERROR: failed to write /etc/resolv.conf"
    failed=1
  fi
fi

[ "$repaired" -eq 1 ] && log "repair complete"

if [ "$first_run_since_boot" -eq 1 ] && [ -x "$DIAG" ]; then
  "$DIAG" POST-REPAIR
fi

trim_log
exit "$failed"
