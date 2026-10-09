#!/bin/sh
# Renders conf/AdGuardHome.yaml from conf/AdGuardHome.yaml.example + .env
#
#   render-config.sh render          write the live config (refuses to clobber)
#   render-config.sh render --force  overwrite an existing live config
#   render-config.sh diff            show how the live config has drifted
set -eu

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
STACK_DIR=$(dirname "$SCRIPT_DIR")
TEMPLATE="${STACK_DIR}/conf/AdGuardHome.yaml.example"
LIVE="${STACK_DIR}/conf/AdGuardHome.yaml"
ENV_FILE="${STACK_DIR}/.env"

VARS="ADGUARD_IP ADGUARD_ADMIN_USER ADGUARD_ADMIN_PASSWORD_HASH LAN_SUBNET LAN_GATEWAY LOCAL_DOMAIN PUBLIC_ZONE NAS_IP"

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"
[ -f "$ENV_FILE" ] || die "no .env in ${STACK_DIR} — copy .env.example and fill it in"

# Read a key without sourcing .env, so an unescaped `$` in the bcrypt hash is
# neither executed nor mangled.
env_get() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" |
    tail -n 1 |
    sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/" -e 's/\r$//'
}

sed_escape() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

build_script() {
  for var in $VARS; do
    val=$(env_get "$var")
    [ -n "$val" ] || die "$var is empty in ${ENV_FILE}"
    printf 's|__%s__|%s|g\n' "$var" "$(sed_escape "$val")"
  done
}

render_to() {
  out="$1"
  # Built into a variable first so a missing .env key aborts before any file
  # is created.
  script=$(build_script)
  printf '%s\n' "$script" > "${out}.sedscript"
  sed -f "${out}.sedscript" "$TEMPLATE" > "$out"
  rm -f "${out}.sedscript"

  # A surviving token means the template grew one that .env and $VARS do not
  # know about — fail rather than start up with a literal token as an upstream.
  if grep -n '__[A-Z0-9_]\{2,\}__' "$out" >/dev/null 2>&1; then
    echo "unsubstituted placeholders:" >&2
    grep -n '__[A-Z0-9_]\{2,\}__' "$out" >&2
    rm -f "$out"
    die "add the missing key to .env and to \$VARS in $(basename "$0")"
  fi
}

case "${1:-render}" in
  render)
    if [ -f "$LIVE" ] && [ "${2:-}" != "--force" ]; then
      die "$LIVE already exists. Run 'render-config.sh diff' first, then pass --force to overwrite."
    fi
    render_to "$LIVE"
    chmod 600 "$LIVE"
    echo "rendered $LIVE"
    echo "next: docker compose up -d"
    ;;

  diff)
    [ -f "$LIVE" ] || die "no live config at $LIVE — run 'render-config.sh render' first"
    TMP="${TMPDIR:-/tmp}/adguard-rendered.$$"
    render_to "$TMP"
    if diff -u "$TMP" "$LIVE" > "${TMP}.diff"; then
      echo "live config matches the template"
      rm -f "$TMP" "${TMP}.diff"
      exit 0
    fi
    echo "--- template (rendered)  +++ live config"
    cat "${TMP}.diff"
    rm -f "$TMP" "${TMP}.diff"
    echo
    echo "AdGuard Home strips comments and expands defaults on its first write,"
    echo "so expect noise. Fold real setting changes back into the template."
    exit 1
    ;;

  *)
    die "usage: $(basename "$0") [render [--force] | diff]"
    ;;
esac
