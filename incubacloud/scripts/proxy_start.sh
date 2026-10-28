#!/usr/bin/env bash
# Start the proxy with the files Full Setup just installed, or fall back
# to the ones it ran with.
#
# Usage:
#   proxy_start.sh keep    # before the new files are moved in
#   proxy_start.sh start
#
# ``keep`` copies the running compose file, static and dynamic
# configuration aside (``.ic-prev-*``). ``start`` brings the proxy up with
# the new ones; when Docker refuses (a port already taken, a file it
# cannot read), the previous three go back, the proxy comes up with them
# and the step fails with Docker's own error. The proxy serves the whole
# host, so Full Setup must never leave it down. A first setup has nothing
# to fall back to and simply fails.
#
# IC_TRAEFIK_DIR overrides ~/traefik (tests).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "proxy_start.sh <keep|start>"

dir="${IC_TRAEFIK_DIR:-$HOME/traefik}"
files=(inverseproxy.yaml traefik.yml config.yml)

compose() {
    (cd "$dir" && docker compose -p inverseproxy -f inverseproxy.yaml "$@")
}

case "$1" in
    keep)
        for f in "${files[@]}"; do
            if [ -f "$dir/$f" ]; then
                cp -p "$dir/$f" "$dir/.ic-prev-$f"
            else
                rm -f "$dir/.ic-prev-$f"
            fi
        done
        ;;
    start)
        status=0
        compose up -d --remove-orphans && compose restart proxy || status=$?
        [ "$status" -eq 0 ] && exit 0
        if [ ! -f "$dir/.ic-prev-inverseproxy.yaml" ]; then
            ic_warn "the proxy did not start and there is nothing to fall back to"
            exit "$status"
        fi
        ic_warn "the proxy did not start (exit $status): putting the previous files back"
        for f in "${files[@]}"; do
            if [ -f "$dir/.ic-prev-$f" ]; then
                cp -p "$dir/.ic-prev-$f" "$dir/$f"
            fi
        done
        mkdir -p "$dir/dynamic"
        cp "$dir/config.yml" "$dir/dynamic/config.yml"
        compose up -d --remove-orphans \
            || ic_warn "the previous files did not start the proxy either"
        exit "$status"
        ;;
    *) ic_die "unknown operation: $1" ;;
esac
