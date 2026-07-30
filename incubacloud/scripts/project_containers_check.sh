#!/usr/bin/env bash
# Refuse to delete a project directory that still has containers.
#
# Usage: project_containers_check.sh <project_dir>
#
# Looks for any docker compose project under <project_dir> that still
# has containers — running *or* stopped. ``docker ps -a`` filtered by
# the compose working-dir label catches strays even when the
# compose.yaml was removed by hand, which a ``docker compose ps`` inside
# the directory would miss.
#
# Exits 0 when the directory is clean (or absent, in which case there is
# nothing to delete), and 1 when something is still there — the caller
# is expected to stop or delete the instances first.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "project_containers_check.sh <project_dir>"

dir="$(ic_expand_home "$1")"

if [ ! -d "$dir" ]; then
    ic_log "project dir $dir not found — nothing to check."
    exit 0
fi

label=com.docker.compose.project.working_dir
resolved="$(readlink -f "$dir")"

# The match must be the directory itself or a path *inside* it. A plain
# prefix test would make a sibling like ``acme-staging`` look as if it
# lived under ``acme`` and block a delete that is perfectly safe.
found="$(
    docker ps -a --filter "label=$label" --format "{{.Label \"$label\"}}" \
        | awk -v d="$resolved" '
            index($0, d) == 1 &&
            (length($0) == length(d) || substr($0, length(d) + 1, 1) == "/")
          ' \
        | head -n1
)"

if [ -n "$found" ]; then
    ic_die "refusing to delete: containers still exist under $found"
fi

ic_log "no containers found under $dir."
