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
# Polls for ~90s (30 × 3s). Prints ``health:ok`` and exits 0 on the
# first healthy response, ``health:timeout`` and exits 1 otherwise; the
# executor asserts on both the exit status and the marker line.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "move_health_wait.sh <instance_dir>"

dir="$(ic_expand_home "$1")"
[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

for _ in $(seq 1 30); do
    if docker compose exec -T odoo \
        curl -sf --max-time 10 http://localhost:8069/web/health \
        >/dev/null 2>&1; then
        echo 'health:ok'
        exit 0
    fi
    sleep 3
done

echo 'health:timeout'
exit 1
