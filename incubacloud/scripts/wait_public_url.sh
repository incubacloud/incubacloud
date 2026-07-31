#!/usr/bin/env bash
# Poll a PUBLIC instance URL until the instance itself answers it.
#
# Usage: wait_public_url.sh <url> <tries> <interval>
#
# Unlike ``wait_http.sh`` (which curls the container from inside the
# host and therefore only proves the process is up), this walks the
# whole public path: DNS → edge → proxy → container. That is what a
# customer's browser does, so it is what "ready" must mean.
#
# A plain HTTP 200 is NOT enough: while the instance's own DNS record
# propagates, the wildcard still resolves to the panel host, whose
# catch-all router serves the "instance is being prepared" page — with
# status 200. So we require Odoo's health payload (``/web/health``
# answers a JSON body containing ``status``); the catch-all page is
# HTML and never matches.
#
# Exits 0 as soon as the instance answers; non-zero on timeout. Callers
# treat the timeout as non-fatal — the instance is healthy either way,
# only its public path is still settling.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" "wait_public_url.sh <url> <tries> <interval>"

url="$1"
tries="$2"
interval="$3"

ic_log "waiting for $url to be served by the instance (up to $tries × ${interval}s)"
for _ in $(seq 1 "$tries"); do
    if curl -fsS --max-time 10 "$url/web/health" 2>/dev/null \
        | grep -q '"status"'; then
        ic_log "public URL is live"
        exit 0
    fi
    sleep "$interval"
done

ic_log "public URL did not answer within the timeout"
exit 1
