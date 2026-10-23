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
#
# Exit codes:
#   0   the operation ran, and so did the steps that follow it
#   24  the operation ran, but what the steps that follow it need could
#       not be read (see ``unchecked`` below); the executors take it for
#       a step done, with a warning (COMPOSE_OP_EXIT_UNCHECKED in
#       models/abstract_executor.py)
#   *   whatever docker compose returned
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

# What each step below may have left wrong, added when what it needs —
# the compose, what runs, odoo's routes — could not be read. A failed
# read is not a stack without the step: taken for one, it would leave
# odoo with the wrong egress and nothing said so (the same compose read
# failed once in the rebuild's boot test, QA, the pull request preview
# walk 6f6, 2026-10-22). The operation has already run by then and did
# what it was asked, so each step that applies still gets its turn, and
# the script ends with EXIT_UNCHECKED once they all had it: not a
# failure, but a warning the job log keeps. The health probe catches
# what is left: an unfiltered route by odoo_net_setup's healthcheck, an
# odoo outside its gatekeeper's network, or with its gatekeeper stopped,
# by the routes odoo has.
unchecked=()
EXIT_UNCHECKED=24

# The exit status of a stop that failed in a restart of the whole stack:
# the steps below still run on what the start brought up, and the script
# ends with it.
stop_status=0

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
    if ! services="$(ic_compose_services)"; then
        ic_warn "odoo_net_setup was not restarted after odoo: if this stack has it, odoo's default route may be the unfiltered one"
        unchecked+=("odoo's default route may be the unfiltered one")
        return 0
    fi
    grep -qx odoo_net_setup <<<"$services" || return 0
    if ! running="$(ic_running_services)"; then
        ic_warn "could not tell whether odoo runs, so odoo_net_setup was not restarted after it: odoo's default route may be the unfiltered one"
        unchecked+=("odoo's default route may be the unfiltered one")
        return 0
    fi
    grep -qx odoo <<<"$running" || return 0
    ic_log "restarting odoo_net_setup after odoo, so odoo's default route is the filtered one"
    docker compose restart odoo_net_setup
}

# Print the interfaces odoo has a route through, other than its
# loopback, one per line: the first column of its /proc/net/route, and
# the device of each line of its /proc/net/ipv6_route that is neither
# link-local nor multicast. An interface alone says nothing: the
# namespace odoo is left in keeps every interface but Docker's — a
# bridge the gatekeeper made, the tunnels the kernel adds to each new
# namespace — and an interface that is up keeps its IPv6 link-local and
# multicast routes there, but it loses every other route. procfs is
# mounted in every container and readable by any user, and the files
# show the namespace of the process reading them, which ``docker compose
# exec`` puts in odoo's; where IPv6 is off, the second one is missing.
# Returns 1 when they could not be read, in 30 seconds at most.
odoo_routed_interfaces() {
    local routes
    routes="$(timeout 30 docker compose exec -T odoo sh -c \
        'cat /proc/net/route && { cat /proc/net/ipv6_route 2>/dev/null || true; }')" \
        || return 1
    grep -q '^Iface' <<<"$routes" || return 1
    awk '
        $1 == "Iface" { next }
        NF == 11 && $1 != "lo" { print $1 }
        NF == 10 && $10 != "lo" && $1 !~ /^(fe[89ab]|ff)/ { print $10 }
    ' <<<"$routes" | sort -u
}

# Whether the compose, read now, declares gatekeeper. Asked only once a
# reading the next step needs has failed, to tell a stack without
# gatekeeper, which has nothing to check, from one the step could not
# check. Succeeds when it declares it, and when it cannot be read either.
may_have_gatekeeper() {
    local services
    services="$(ic_compose_services)" || return 0
    grep -qx gatekeeper <<<"$services"
}

# gatekeeper, the egress of a staging rendered by template v10, owns the
# network namespace odoo runs in (network_mode: service:gatekeeper) and
# gets a new one every time it starts, while a running odoo stays in the
# one it joined, left without a route: no database, and nothing reaches
# it. A restart of gatekeeper by name leaves it so, and so do a recreated
# gatekeeper and an ``up`` that starts a stopped one (Docker 29, Compose
# 5). So once the operation is done, odoo is asked which interfaces it
# has a route through, and restarted when there is none but its
# loopback, which puts it in the namespace gatekeeper has now. Neither
# the compose nor which services the operation named is asked: odoo on
# a network of its own always has a route, so a stack without
# gatekeeper is left as it is, as is an odoo not running. Only when a
# reading fails is the compose read, and a stack it shows without
# gatekeeper is left as it is too.
rejoin_gatekeeper() {
    local running interfaces
    if ! running="$(ic_running_services)"; then
        may_have_gatekeeper || return 0
        ic_warn "could not tell whether odoo runs, so its network was not checked: if this stack has a gatekeeper, odoo may be left in its old namespace, with no network at all"
        unchecked+=("odoo may be left in gatekeeper's old namespace, with no network at all")
        return 0
    fi
    grep -qx odoo <<<"$running" || return 0
    if ! interfaces="$(odoo_routed_interfaces)"; then
        may_have_gatekeeper || return 0
        ic_warn "could not read odoo's routes, so its network was not checked: if this stack has a gatekeeper, odoo may be left in its old namespace, with no network at all"
        unchecked+=("odoo may be left in gatekeeper's old namespace, with no network at all")
        return 0
    fi
    [ -z "$interfaces" ] || return 0
    ic_log "odoo has no route but its loopback: restarting it, so it runs in the network gatekeeper has now"
    docker compose restart odoo
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
        if [ "$#" -eq 0 ]; then
            # Stopped and started, not restarted: ``docker compose
            # restart`` restarts every container at once, and odoo, which
            # joins gatekeeper's namespace, lost the race to a gatekeeper
            # not running yet every time: odoo left exited ("cannot join
            # network namespace of a non running container", 5 of 5 on
            # Docker 29, Compose 5), or running in the old namespace.
            # ``start`` goes in dependency order and waits for the
            # services odoo depends on to be healthy. A stop that fails
            # still gets its start and the steps below, so the stack is
            # not left half down; the script ends with the stop's status.
            docker compose stop || stop_status=$?
            docker compose start
        else
            docker compose restart "$@"
        fi
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
# A stop or a down only take networks away: after one, odoo is not
# restarted into a gatekeeper that may be the very service stopped.
case "$op" in
    up | start | restart)
        rejoin_gatekeeper
        ;;
esac
if [ "${#unchecked[@]}" -gt 0 ]; then
    risks="$(printf '%s; ' "${unchecked[@]}")"
    ic_warn "the $op ran, but the steps that follow it could not be checked (see the warnings above): until the instance is restarted, ${risks%; }"
fi
[ "$stop_status" -eq 0 ] || exit "$stop_status"
[ "${#unchecked[@]}" -eq 0 ] || exit "$EXIT_UNCHECKED"
