#!/usr/bin/env bash
# Remove a service block from one or more docker-compose files.
#
# Usage: strip_compose_service.sh <instance_dir> <service> <file...>
#
# The copier template always renders the ``smtp`` and ``backup`` service
# blocks, but without an image (no relay / no backend configured) they
# make ``docker compose`` fail. This removes the named block: the line
# "  <service>:" and every following line at deeper indent (or blank)
# until the next top-level service key.
#
# Shared on purpose — deploy, rebuild and warm-claim all strip the same
# ``smtp`` block, which used to be three copies of the same awk. A file
# that does not exist is skipped, so callers can pass every file that
# *might* contain the service.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" \
    "strip_compose_service.sh <instance_dir> <service> <file...>"

dir="$(ic_expand_home "$1")"
service="$2"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

for f in "$@"; do
    [ -f "$f" ] || continue
    ic_log "stripping the '$service' service from $f"
    awk -v svc="$service" '
        $0 ~ ("^  " svc ":") { skip = 1; next }
        skip && /^  [a-z]/   { skip = 0 }
        skip && /^[^ ]/      { skip = 0 }
        !skip
    ' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
