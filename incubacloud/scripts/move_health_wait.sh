#!/usr/bin/env bash
# Wait for a freshly (re)deployed instance to answer its health endpoint.
#
# Usage: move_health_wait.sh <instance_dir>
#
# Used by MoveCutoverExecutor on the destination host: the cutover only
# proceeds once Odoo answers /web/health, so a stack that never comes up
# fails the job — which breaks the move chain and leaves the source
# untouched and recoverable.
#
# Prints ``health:ok`` and exits 0 on the first healthy response,
# ``health:timeout`` and exits 1 otherwise; the executor asserts on both
# the exit status and the marker line.
#
# Values (16-nov-2026):
# - a probe every 0.5 s: this wait is inside a move's cut, and the cut is
#   measured to the half second (it probed every 3 s through
#   ``docker compose exec``, which added up to 3 s and the compose start-up
#   to every cut);
# - ``curl --max-time 2``: a probe that hangs must not hide the cadence;
# - 120 s in all (``MOVE_HEALTH_DEADLINE_S`` shortens it under test): the
#   30 rounds of a probe and 3 s it replaces took 100-105 s when Odoo did
#   not answer, so no stack that came up in time before is given up on
#   now. Giving up fails the cutover, and the move is rolled back.
# The container is looked up once and probed with ``docker exec``. It is
# looked up again when the probe fails for want of it (docker's own exit
# statuses, 1 and 125 and above, rather than curl's 7, 22 and 28) and
# every 5 s of failures in any case, so a container recreated meanwhile
# is found.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "move_health_wait.sh <instance_dir>"

dir="$(ic_expand_home "$1")"
[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

deadline="${MOVE_HEALTH_DEADLINE_S:-120}"
SECONDS=0
id=""
found_at=0
while :; do
    if [ -z "$id" ]; then
        id="$(docker compose ps -q odoo 2>/dev/null | head -n 1)" || id=""
        found_at=$SECONDS
    fi
    if [ -n "$id" ]; then
        status=0
        docker exec "$id" \
            curl -sf --max-time 2 http://localhost:8069/web/health \
            >/dev/null 2>&1 || status=$?
        if [ "$status" -eq 0 ]; then
            echo 'health:ok'
            exit 0
        fi
        case "$status" in
            7 | 22 | 28) ;;
            *) id="" ;;
        esac
        if [ -n "$id" ] && ((SECONDS - found_at >= 5)); then
            id=""
        fi
    fi
    ((SECONDS < deadline)) || break
    sleep 0.5
done

echo 'health:timeout'
exit 1
