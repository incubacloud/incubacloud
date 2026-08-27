#!/usr/bin/env bash
# Empty an instance's backup prefix before its stack is torn down.
#
# Usage: backup_purge.sh <instance_dir>
#
# The invariant this serves: never leave objects belonging to an
# instance that no longer exists. The only thing that can talk to the
# bucket is the instance's own ``backup`` container — it holds the
# credentials, the endpoint and the destination in ``.docker/backup.env``
# — and ``compose down -v`` destroys it. So the purge has to run
# *before* the teardown, from here, and if it cannot the caller must
# abort rather than strand the objects with nothing left to delete them.
#
# ``run --rm`` rather than ``exec``: it builds the container from the
# compose definition without needing the stack up, so a stopped instance
# purges just as well as a running one.
#
# duplicity cannot do this itself. ``remove-all-but-n-full`` requires
# n>=1 and there is no command that removes the last full, so the delete
# goes through the S3 API from inside the container, reusing the very
# same environment duplicity would have used. The program that does it
# lives in ``lib/purge_prefix.sh``, shared with the ephemeral purge of
# an archived instance so both empty a prefix the exact same way.
#
# Exit codes are the contract with the executor, which classifies the
# failure from the code alone. Matching duplicity/boto text would rot the
# moment either changes version and silently downgrade every diagnosis to
# "unknown":
#
#   0   purged — the prefix is empty and was verified empty
#   10  nothing to purge — the prefix was already empty (not a failure:
#       the invariant already holds, so the caller proceeds)
#   20  no ``backup`` service in this compose (panel/host drift)
#   21  the provider rejected the credentials
#   22  the container could not start, or any other failure
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"
# shellcheck source-path=SCRIPTDIR source=lib/purge_prefix.sh
source "$(dirname "$0")/lib/purge_prefix.sh"

ic_require_args 1 "$#" "backup_purge.sh <instance_dir>"

dir="$(ic_expand_home "$1")"

if [ ! -d "$dir" ]; then
    # No directory means no compose and no credentials. The caller only
    # sends us here when the panel says this instance has backups, so a
    # missing directory is drift, not an empty prefix.
    ic_warn "instance directory not found: $dir"
    exit 20
fi

cd "$dir"

if ! docker compose config --services 2>/dev/null | grep -qx backup; then
    # Deliberately checked here and not inferred from a failed exec:
    # ``docker compose exec`` answers `service "backup" is not running`
    # both when the service is stopped and when it does not exist, so
    # that message can never tell the two apart.
    ic_warn "no 'backup' service declared in $dir"
    exit 20
fi

ic_log "purging backup prefix via the instance's own backup container"

set +e
ic_purge_prefix_program \
    | docker compose run --rm -T --entrypoint python3 backup -
rc=$?
set -e

case "$rc" in
    0)  ic_log "backup prefix purged." ;;
    10) ic_log "backup prefix was already empty." ;;
    21) ic_warn "storage rejected the credentials." ;;
    20) ic_warn "backup service unavailable." ;;
    *)  ic_warn "backup purge failed (exit $rc)."
        # Anything the inner script did not classify — most often the
        # container failing to start at all, e.g. the 64-byte hostname
        # limit — lands here as 22.
        rc=22 ;;
esac

exit "$rc"
