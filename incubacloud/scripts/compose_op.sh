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
