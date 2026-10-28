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
# cannot read), or the proxy is not running a few seconds later (Traefik
# exits on a static file it cannot parse, and Docker reports nothing),
# the previous three go back, the proxy comes up with them and the step
# fails. The proxy serves the whole host, so Full Setup must never leave
# it down. A first setup has nothing to fall back to and simply fails.
#
# IC_TRAEFIK_DIR overrides ~/traefik and IC_PROXY_SETTLE the seconds
# waited before the check (tests).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "proxy_start.sh <keep|start>"

dir="${IC_TRAEFIK_DIR:-$HOME/traefik}"
files=(inverseproxy.yaml traefik.yml config.yml)

compose() {
    (cd "$dir" && docker compose -p inverseproxy -f inverseproxy.yaml "$@")
}

# Whether the proxy container is running (not exited, not restarting).
running() {
    local id
    id="$(compose ps -q proxy)" || return 1
    [ -n "$id" ] || return 1
    [ "$(docker inspect -f '{{.State.Status}}' "$id")" = "running" ]
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
        if [ "$status" -eq 0 ]; then
            sleep "${IC_PROXY_SETTLE:-5}"
            running && exit 0
            ic_warn "the proxy started and stopped: its new files are not valid"
            status=1
        fi
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
