#!/usr/bin/env bash
# Poll an in-container HTTP endpoint until it answers.
#
# Usage: wait_http.sh <instance_dir> <path> <tries> <interval>
#
# Runs ``curl`` inside the odoo container against localhost:8069<path>,
# retrying up to <tries> times every <interval> seconds. Exits 0 as soon
# as the endpoint answers, non-zero if it never does — so the caller can
# stop_on_failure a start that hangs instead of waiting forever.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 4 "$#" "wait_http.sh <instance_dir> <path> <tries> <interval>"

dir="$(ic_expand_home "$1")"
path="$2"
tries="$3"
interval="$4"

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

ic_log "waiting for odoo to answer $path (up to $tries × ${interval}s)"
for _ in $(seq 1 "$tries"); do
    if docker compose exec -T odoo \
        curl -fsS "http://localhost:8069$path" >/dev/null 2>&1; then
        exit 0
    fi
    sleep "$interval"
done

ic_die "odoo did not answer $path within the timeout"
