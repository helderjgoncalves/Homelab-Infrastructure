#!/bin/sh
# Run the updater. The stacks directory is the parent of this file.
# Cron, NAS local time:
# 0 9 * * *  update.sh
# 0 12 * * 0 update.sh --digest
cd "$(dirname "$0")" || exit 1
here=$(pwd)
self="$here/$(basename "$0")"
stacks=$(CDPATH= cd -- "$here/.." && pwd) || exit 1

docker_bin=${DOCKER:-}
if [ -z "$docker_bin" ]; then
  for candidate in /share/CACHEDEV2_DATA/.qpkg/container-station/bin/docker /usr/bin/docker; do
    if [ -x "$candidate" ]; then
      docker_bin=$candidate
      break
    fi
  done
fi
if [ -z "$docker_bin" ]; then
  echo "docker not found" >&2
  exit 1
fi

# Plex is a NAS package. Its version is read here and handed to Python.
plex_version=$(curl -fsS -m 5 http://127.0.0.1:32400/identity 2>/dev/null | sed -n 's/.*MediaContainer.*version="\([^"]*\)".*/\1/p')
export PLEX_VERSION="$plex_version"
# Sunday mail and the seven-day retry use the NAS clock, not the container clock.
export UPDATE_WDAY=$(date +%u)
export UPDATE_TODAY=$(date +%F)

# Cron on this NAS runs as admin. Keep the files the job writes with the
# owner of this script, who can read .env.
if [ "$(id -u)" -eq 0 ]; then
  run_uid=$(stat -c %u "$self") || exit 1
  run_gid=$(stat -c %g "$self") || exit 1
else
  run_uid=$(id -u)
  run_gid=$(id -g)
fi

# Cron appends to state/cron.log as root. Keep it short, and owned like the rest of state/.
# Rewrite in place: the cron shell holds the file open for append.
log_file="$here/state/cron.log"
if [ "$(id -u)" -eq 0 ] && [ -f "$log_file" ]; then
  if [ "$(wc -c < "$log_file")" -gt 2097152 ]; then
    tail -n 5000 "$log_file" > "$log_file.tmp" && cat "$log_file.tmp" > "$log_file"
    rm -f "$log_file.tmp"
  fi
  chown "$run_uid:$run_gid" "$log_file" 2>/dev/null
fi

# Uptime Kuma push monitor. Sent from here, not from Python: Kuma listens on the
# NAS loopback, and a run that never starts must not count as a good one.
# Exit 0 means the run finished and any mail went out.
heartbeat() {
  code=$1
  shift
  for arg in "$@"; do
    case $arg in --dry-run | --check | -h | --help) return 0 ;; esac
  done
  token=$(sed -n 's/^KUMA_PUSH_TOKEN=//p' "$here/.env" 2>/dev/null | head -n 1 | tr -d "\"' \r")
  [ -n "$token" ] || return 0
  if [ "$code" -eq 0 ]; then
    query="status=up&msg=OK"
  else
    query="status=down&msg=update.sh%20exited%20$code"
  fi
  curl -fsS -m 10 -o /dev/null "http://127.0.0.1:3001/api/push/$token?$query" ||
    echo "heartbeat to Uptime Kuma failed" >&2
}

if [ "$(id -u)" -ne 0 ] && command -v python3 >/dev/null 2>&1; then
  python3 "$here/update.py" "$@"
else
  "$docker_bin" run --rm \
    --user "$run_uid:$run_gid" \
    --group-add 0 \
    -e PLEX_VERSION \
    -e UPDATE_WDAY \
    -e UPDATE_TODAY \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$stacks:$stacks" \
    -w "$here" \
    python:3.14-alpine \
    python "$here/update.py" "$@"
fi
code=$?
heartbeat "$code" "$@"
exit "$code"
