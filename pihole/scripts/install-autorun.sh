#!/bin/sh
# -----------------------------------------------------------------------------
# Install the QNAP boot hook onto the DOM config partition.
# -----------------------------------------------------------------------------
# Must run as root:  sudo ./install-autorun.sh
#
# QTS executes exactly one user startup script: /tmp/config/autorun.sh, read
# from the DOM config partition during init_nas.sh, and only when Control Panel
# -> Hardware -> "Run user defined processes during startup" is ticked.
#
# The partition is derived the same way init_nas.sh derives it:
#   BOOT_DEV=$(hal_app --get_boot_pd port_id=0)   ->  /dev/mmcblk0p
#   DEV_NAS_CONFIG=${BOOT_DEV}6                   ->  /dev/mmcblk0p6
# (the ${BOOT_DEV}5 / ${BOOT_DEV}7 branches are ARM-only)
#
# Idempotent: re-run to update the installed copy.
# -----------------------------------------------------------------------------
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SOURCE="${SCRIPT_DIR}/autorun.sh"
STAGED=$(mktemp)
trap 'rm -f "$STAGED"' EXIT
MOUNTPOINT=/tmp/_autorun_install

if [ "$(id -u)" != "0" ]; then
  echo "Error: must run as root (sudo $0)" >&2
  exit 1
fi

[ -f "$SOURCE" ] || { echo "Error: ${SOURCE} not found" >&2; exit 1; }

# --- Resolve host-specific paths --------------------------------------------
# The tracked autorun.sh ships with placeholders instead of absolute paths, so
# nothing about this NAS's directory layout gets committed. Fill them in here:
#   __PIHOLE_WATCHDOG__ — derived from SCRIPT_DIR (this install lives next to
#                        the watchdog by definition).
#   __SWAP_SCRIPT__     — optional AUTORUN_SWAP_SCRIPT from ../.env; empty
#                        string means "no extra script", autorun.sh skips it.
PIHOLE_WATCHDOG="${SCRIPT_DIR}/pihole-watchdog.sh"
[ -x "$PIHOLE_WATCHDOG" ] || { echo "Error: ${PIHOLE_WATCHDOG} not executable" >&2; exit 1; }

AUTORUN_SWAP_SCRIPT=""
if [ -f "${SCRIPT_DIR}/../.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${SCRIPT_DIR}/../.env"
  set +a
fi
SWAP_SCRIPT_VALUE="${AUTORUN_SWAP_SCRIPT:-}"

sed -e "s|__PIHOLE_WATCHDOG__|${PIHOLE_WATCHDOG}|g" \
    -e "s|__SWAP_SCRIPT__|${SWAP_SCRIPT_VALUE}|g" \
    "$SOURCE" > "$STAGED"
sh -n "$STAGED" || { echo "Error: staged autorun.sh has a syntax error" >&2; exit 1; }

# sh -n cannot catch this: an unsubstituted placeholder is still valid shell, so
# a forgotten sed would install a hook that polls for five minutes and then logs
# "never appeared", with nothing pointing at the cause.
if grep -q '__[A-Z_][A-Z_]*__' "$STAGED"; then
  echo "Error: unsubstituted placeholder(s) in staged autorun.sh:" >&2
  grep -o '__[A-Z_][A-Z_]*__' "$STAGED" | sort -u | sed 's/^/  /' >&2
  exit 1
fi

# --- Locate the config partition, exactly as init_nas.sh does ----------------
if [ -x /sbin/hal_app ]; then
  BOOT_DEV=$(/sbin/hal_app --get_boot_pd port_id=0)
else
  echo "Error: /sbin/hal_app missing — cannot determine the boot device" >&2
  exit 1
fi
DEV_NAS_CONFIG="${BOOT_DEV}6"

if [ ! -b "$DEV_NAS_CONFIG" ]; then
  echo "Error: ${DEV_NAS_CONFIG} is not a block device" >&2
  exit 1
fi
echo "Config partition: ${DEV_NAS_CONFIG}"

# --- Warn if the feature is switched off -------------------------------------
if [ "$(/sbin/getcfg Misc Autorun -d 0)" != "TRUE" ]; then
  echo "WARNING: Misc/Autorun is not TRUE — QTS will ignore this file."
  echo "         Tick Control Panel -> Hardware -> 'Run user defined"
  echo "         processes during startup'."
fi

# --- Install ------------------------------------------------------------------
mkdir -p "$MOUNTPOINT"
mount -t ext2 "$DEV_NAS_CONFIG" "$MOUNTPOINT"
trap 'umount "$MOUNTPOINT" 2>/dev/null || true; rmdir "$MOUNTPOINT" 2>/dev/null || true; rm -f "$STAGED"' EXIT

if [ -f "${MOUNTPOINT}/autorun.sh" ]; then
  cp "${MOUNTPOINT}/autorun.sh" "${MOUNTPOINT}/autorun.sh.bak"
  echo "Existing autorun.sh backed up to autorun.sh.bak on the DOM."
fi

cp "$STAGED" "${MOUNTPOINT}/autorun.sh"
chmod +x "${MOUNTPOINT}/autorun.sh"
sync

echo "Installed:"
ls -l "${MOUNTPOINT}/autorun.sh"
echo
echo "Verifying it is executable and syntactically valid..."
sh -n "${MOUNTPOINT}/autorun.sh" && echo "  syntax OK"
[ -x "${MOUNTPOINT}/autorun.sh" ] && echo "  executable OK"

echo
echo "Done. On the next boot it will appear in /tmp/autorun-boot.log."
