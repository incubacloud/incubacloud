#!/usr/bin/env bash
# Put the edge guard in the proxy's dynamic directory, and say whether it holds.
#
# Usage: edge_guard.sh install <staged> [--no-mount-check] [--no-probe]
#
# The guard (``ic_guard.yml``) is the router that has the host's Traefik
# refuse Odoo's /website/info on every instance (cloud_host_edge_guard.py).
# The file is replaced only when it differs, and atomically: copied next
# to the watched directory, then renamed into it, because Traefik's file
# watcher reads a half-written file as it finds it. A directory left to
# root by a host set up by hand takes ``sudo -n``. The staged copy is
# removed whatever happens.
#
# Then, unless told not to:
#
# * the proxy has to mount the directory, or it never reads the file: the
#   file stays written and the script exits 3, the next Full Setup mounts
#   it. Full Setup itself writes the guard before it starts the proxy, so
#   it passes ``--no-mount-check``;
# * the proxy is asked for the page on 127.0.0.1, up to five times two
#   seconds apart: a 403 exits 0; a handshake the CDN's client-certificate
#   requirement refuses (code 000) exits 4 ("not verified": the QA checks
#   it from outside); a real HTTP answer other than 403 (200, 404...) exits
#   5, because the guard is not applied (Traefik dropped the file, say).
#
# Prints ``ic_guard.yml unchanged`` or ``ic_guard.yml updated``, then
# ``IC_EDGE_GUARD_NOT_MOUNTED``, ``IC_EDGE_GUARD_REFUSES``,
# ``IC_EDGE_GUARD_UNVERIFIED <last answer>`` or
# ``IC_EDGE_GUARD_NOT_APPLIED <last answer>``.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

usage="edge_guard.sh install <staged> [--no-mount-check] [--no-probe]"
ic_require_args 2 "$#" "$usage"
[ "$1" = install ] || ic_die "unknown operation: $1. Usage: $usage"
staged="$2"
shift 2
mount_check=1
probe=1
for arg in "$@"; do
    case "$arg" in
        --no-mount-check) mount_check=0 ;;
        --no-probe) probe=0 ;;
        *) ic_die "unknown option: $arg. Usage: $usage" ;;
    esac
done

traefik="${IC_TRAEFIK_DIR:-$HOME/traefik}"
target="$traefik/dynamic/ic_guard.yml"
partial="$traefik/.ic_guard.yml.new"
proxy="${IC_PROXY_CONTAINER:-inverseproxy-proxy-1}"

cleanup() {
    rm -f "$staged" 2>/dev/null || true
    rm -f "$partial" 2>/dev/null || sudo -n rm -f "$partial" 2>/dev/null || true
}
trap cleanup EXIT

[ -f "$staged" ] || ic_die "the staged guard is not there: $staged"

mkdir -p "$traefik/dynamic" 2>/dev/null || sudo -n mkdir -p "$traefik/dynamic"
if cmp -s "$staged" "$target"; then
    echo "ic_guard.yml unchanged"
else
    if ! { cp "$staged" "$partial" && mv -f "$partial" "$target"; } 2>/dev/null; then
        if ! { sudo -n cp "$staged" "$partial" && sudo -n mv -f "$partial" "$target"; }; then
            ic_die "could not write $target"
        fi
    fi
    echo "ic_guard.yml updated"
fi

if [ "$mount_check" -eq 1 ]; then
    mounts="$(docker inspect "$proxy" \
        --format '{{range .Mounts}}{{.Destination}} {{end}}' 2>/dev/null || true)"
    if ! grep -q "/etc/traefik/dynamic " <<<"$mounts"; then
        echo "IC_EDGE_GUARD_NOT_MOUNTED the proxy does not read $traefik/dynamic; the next Full Setup mounts it"
        exit 3
    fi
fi

if [ "$probe" -eq 1 ]; then
    answer=""
    for attempt in 1 2 3 4 5; do
        answer="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 \
            -H 'Host: ic-guard.invalid' https://127.0.0.1/website/info || true)"
        if [ "$answer" = 403 ]; then
            echo "IC_EDGE_GUARD_REFUSES"
            exit 0
        fi
        [ "$attempt" -lt 5 ] && sleep "${IC_EDGE_GUARD_PROBE_WAIT:-2}"
    done
    case "${answer:-000}" in
        000)
            echo "IC_EDGE_GUARD_UNVERIFIED ${answer:-none}"
            exit 4
            ;;
        *)
            echo "IC_EDGE_GUARD_NOT_APPLIED $answer"
            exit 5
            ;;
    esac
fi
