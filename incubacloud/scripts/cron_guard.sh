#!/usr/bin/env bash
# Run click-odoo-update with the instance's crons paused.
#
# Usage: cron_guard.sh update <instance_dir> <db> <pg_user> [tries] [interval]
#
# Odoo refuses to modify an ``ir_cron`` row while its job is executing
# (``lock_for_update`` uses SKIP LOCKED and raises), so a module update
# that ships a cron record dies with a ParseError whenever a tick is in
# flight — a coin flip on every rebuild.
#
# Pausing has to skip the running rows too: a plain ``UPDATE`` on a row
# a cron holds would block until the job finishes, turning a fast,
# clean failure into a hung deploy. So each pass grabs whatever is free
# (``FOR UPDATE SKIP LOCKED``), records it, and deactivates it; rows
# still held by a running tick stay active and are retried on the next
# pass, once their job has finished. Deactivating first is what makes
# it converge: the scheduler cannot start anything new.
#
# Exit codes:
#   0   update ran (its own status is propagated)
#   75  crons were still running after the budget — nothing was updated,
#       the caller reschedules the job instead of failing it
#   *   whatever click-odoo-update returned
#
# The paused ids live in a marker table inside the instance DB so a
# crashed run can be recovered by the next one; an EXIT trap restores
# them on every normal path, including failure and a stop (TERM, INT,
# HUP), and on a stop it first removes the update's container. A KILL
# leaves them paused, with their list, for the next run.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 4 "$#" \
    "cron_guard.sh update <instance_dir> <db> <pg_user> [tries] [interval]"

op="$1"
dir="$(ic_expand_home "$2")"
db="$3"
pg_user="$4"
tries="${5:-12}"
interval="${6:-15}"

[ "$op" = "update" ] || ic_die "unknown operation: $op"
[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

MARKER="incubacloud_cron_pause"
EXIT_CRONS_BUSY=75

# Every query is best-effort: a stopped instance (a sleeping one)
# has no database to talk to, and no crons running either — in that
# case the guard simply gets out of the way.
_psql() {
    docker compose exec -T db psql -U "$pg_user" -d "$db" -tAc "$1" 2>/dev/null
}

# Put the paused crons back and drop their list. One ``psql -c`` runs both
# statements in one transaction: both are done or neither is, and a list
# that is kept is what the next run restores the crons from.
_resume() {
    _apart docker compose exec -T db psql -U "$pg_user" -d "$db" -tAc \
        "UPDATE ir_cron SET active = true
         WHERE id IN (SELECT id FROM $MARKER);
         DROP TABLE IF EXISTS $MARKER" >/dev/null 2>&1 || true
}

# The name of the update's container, set before it starts.
update_container=""

# Remove the update's container, if one was started. On a stop, ``docker
# compose run`` passes the TERM on to the container and leaves at once,
# and click-odoo-update, its PID 1 with no handler, ignores it: left
# there, the update would go on with the crons running again. After an
# update that ended, there is no such container any more.
# shellcheck disable=SC2317  # reached through the EXIT trap below
_remove_update() {
    [ -n "$update_container" ] || return 0
    _apart docker rm -f "$update_container" >/dev/null 2>&1 || true
}

if ! _psql "SELECT 1" >/dev/null; then
    ic_log "instance database unreachable — running the update unguarded"
    ic_compose_run --rm odoo click-odoo-update --database "$db"
    exit $?
fi

# A previous run may have died between pause and resume.
_resume
_psql "CREATE TABLE IF NOT EXISTS $MARKER (id integer PRIMARY KEY)" >/dev/null
# Run apart (``detached_step.sh``), a stop sends TERM to the step's whole
# session and the ``timeout`` that runs the step sends it once more to its
# process group: this shell gets it two or three times. Left to bash's
# default, one that lands while bash goes into the trap kills it there,
# and the crons stay paused. Handled, the first TERM leaves through the
# trap, and the trap ignores the others; its docker calls, which do not
# keep an ignored TERM, run apart (``_apart``).
trap 'trap "" TERM INT HUP; _remove_update; _resume' EXIT
trap 'trap "" TERM INT HUP; exit 143' TERM INT HUP

ic_log "pausing crons (up to $tries × ${interval}s for in-flight ticks)"
for _ in $(seq 1 "$tries"); do
    # One transaction: pick every cron nobody is running, remember it,
    # switch it off.
    _psql "WITH picked AS (
               SELECT id FROM ir_cron WHERE active FOR UPDATE SKIP LOCKED
           ), saved AS (
               INSERT INTO $MARKER (id) SELECT id FROM picked
               ON CONFLICT DO NOTHING
           )
           UPDATE ir_cron SET active = false
           WHERE id IN (SELECT id FROM picked)" >/dev/null || true

    left="$(_psql "SELECT count(*) FROM ir_cron WHERE active" || echo 0)"
    if [ "${left:-0}" -eq 0 ]; then
        ic_log "no cron is running — updating"
        update_container="incubacloud-update-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
        ic_compose_run --name "$update_container" --rm odoo \
            click-odoo-update --database "$db"
        exit $?
    fi
    ic_log "$left cron(s) still executing — waiting ${interval}s"
    sleep "$interval"
done

busy="$(_psql "SELECT string_agg(cron_name, ', ')
               FROM ir_cron WHERE active" || echo '?')"
ic_log "crons still executing after the budget: ${busy:-?}"
exit "$EXIT_CRONS_BUSY"
