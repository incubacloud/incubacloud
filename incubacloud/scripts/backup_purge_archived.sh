#!/usr/bin/env bash
# Empty an archived instance's backup prefix, with no stack to run in.
#
# Usage: backup_purge_archived.sh <env_file> <image>
#
# An archived instance has already been torn down: its compose project,
# its containers and its directory are gone, and only the chain in the
# bucket is left. Deleting the record therefore has to delete that chain
# too — the invariant is that objects never outlive the instance they
# belong to — but there is no ``backup`` service left to run the purge
# from, which is what ``backup_purge.sh`` relies on.
#
# So the container is created for this one command and thrown away. The
# alternative would be to redeploy the instance just to tear it down
# again: a full deploy — domains, Traefik, minutes of host resources —
# with all of its own failure modes, to delete something. The only cost
# of the ephemeral route is that archiving ran ``down --rmi all``, so the
# host may have to pull the image again.
#
# The environment comes from a file the panel uploads (destination,
# credentials, endpoint), never from arguments: an argument would be
# visible in ``ps`` to every account on the host for as long as the
# container runs. The file is removed on the way out, including when
# this script fails.
#
# Exit codes are the contract with the executor:
#
#   0   purged — the prefix is empty and was verified empty
#   10  nothing to purge — the prefix was already empty
#   20  the environment file is missing (panel/host drift)
#   21  the provider rejected the credentials
#   22  the container could not start, or any other failure
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"
# shellcheck source-path=SCRIPTDIR source=lib/purge_prefix.sh
source "$(dirname "$0")/lib/purge_prefix.sh"

ic_require_args 2 "$#" "backup_purge_archived.sh <env_file> <image>"

env_file="$1"
image="$2"

# Removed whatever happens: it holds the destination's credentials, and
# a failed run must not leave them on the host's /tmp. The directory
# goes too, with a plain rmdir — it is empty by then, and a recursive
# delete of a path that arrived as an argument is not a thing this
# script should be able to do.
trap 'rm -f "$env_file"; rmdir "$(dirname "$env_file")" 2>/dev/null || true' EXIT

if [ ! -f "$env_file" ]; then
    ic_warn "environment file not found: $env_file"
    exit 20
fi

ic_log "purging archived backup prefix via an ephemeral container"

set +e
ic_purge_prefix_program \
    | docker run --rm -i --env-file "$env_file" --entrypoint python3 \
        "$image" -
rc=$?
set -e

case "$rc" in
    0)  ic_log "archived backup prefix purged." ;;
    10) ic_log "archived backup prefix was already empty." ;;
    21) ic_warn "storage rejected the credentials." ;;
    20) ic_warn "environment unavailable." ;;
    *)  ic_warn "archived backup purge failed (exit $rc)."
        # Anything the inner program did not classify — most often the
        # image failing to pull or to start at all — lands here as 22.
        rc=22 ;;
esac

exit "$rc"
