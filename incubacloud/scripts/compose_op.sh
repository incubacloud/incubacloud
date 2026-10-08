#!/usr/bin/env bash
# Run a docker compose lifecycle operation on an instance directory.
#
# Usage: compose_op.sh <instance_dir> <up|start|stop|restart|down> [service...]
#
# With no service names the operation applies to the whole stack; the
# move flow passes an explicit list to quiesce ``odoo`` while ``db`` and
# ``backup`` keep running.
#
# ``down`` tolerates a missing directory: tearing down an instance whose
# folder is already gone is a success, not a failure. The other
# operations require it — a missing directory there means the caller is
# out of sync with the host, and silently doing nothing would report a
# started instance that does not exist.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" \
    "compose_op.sh <instance_dir> <up|start|stop|restart|down> [service...]"

dir="$(ic_expand_home "$1")"
op="$2"
shift 2

if [ ! -d "$dir" ]; then
    if [ "$op" = "down" ]; then
        ic_log "directory $dir not found, nothing to shut down."
        exit 0
    fi
    ic_die "instance directory not found: $dir"
fi

cd "$dir"

# Whether the operation may start odoo: it names no service (the whole
# stack) or names odoo among them.
#   $@  the service names the caller passed
starts_odoo() {
    [ "$#" -eq 0 ] && return 0
    local svc
    for svc in "$@"; do
        [ "$svc" = "odoo" ] && return 0
    done
    return 1
}

# odoo_net_setup, the egress sidecar of a staging with an allowlist,
# shares odoo's network namespace and replaces its default route once,
# when the sidecar starts. Starting odoo again hands it a fresh namespace
# with Docker's own route, straight out, while the sidecar stays up in the
# old one: a stop and start of odoo (a restore) did it every time, and a
# restart that started the sidecar before odoo did it on a host
# (2026-10-08). So once odoo runs, the sidecar is restarted, which
# replaces the route in the namespace odoo has now. A stack without the
# sidecar, or with odoo not running, is left as it is.
reinject_egress_route() {
    local services running
    services="$(docker compose config --services 2>/dev/null || true)"
    grep -qx odoo_net_setup <<<"$services" || return 0
    running="$(docker compose ps --status running --services 2>/dev/null || true)"
    grep -qx odoo <<<"$running" || return 0
    ic_log "restarting odoo_net_setup after odoo, so odoo's default route is the filtered one"
    docker compose restart odoo_net_setup
}

case "$op" in
    up)
        ic_log "starting containers in $dir"
        docker compose up -d "$@"
        ;;
    start)
        ic_log "starting existing containers in $dir"
        docker compose start "$@"
        ;;
    stop)
        ic_log "stopping containers in $dir"
        docker compose stop "$@"
        ;;
    restart)
        ic_log "restarting containers in $dir"
        docker compose restart "$@"
        ;;
    down)
        ic_log "removing containers, images and volumes in $dir"
        docker compose down --rmi all -v --remove-orphans "$@"
        ;;
    *)
        ic_die "unknown operation: $op (expected up, start, stop, restart or down)"
        ;;
esac

case "$op" in
    up | start | restart)
        if starts_odoo "$@"; then
            reinject_egress_route
        fi
        ;;
esac
