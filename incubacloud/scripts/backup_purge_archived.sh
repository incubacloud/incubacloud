#!/usr/bin/env bash
# Empty an archived instance's backup prefix, with no stack to run in.
#
# Usage: backup_purge_archived.sh <env_file> <image> [<env_file>...]
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
# credentials, endpoint, bounds), never from arguments: an argument would
# be visible in ``ps`` to every account on the host for as long as the
# container runs. The file is removed on the way out, including when
# this script fails.
#
# Two bounds, read by ``lib/purge_prefix.sh``: ``PURGE_BEFORE``, the
# instant the delete was decided, or the creation of an instance that
# took the name since when earlier, so what that one stores stays; and
# ``PURGE_SINCE``, set when this instance took the name of an earlier one
# that holds the same path: its own creation, so what the earlier one
# stored stays for that one's purge.
#
# Each further file is one more prefix of the same copy, emptied the
# same way in a container of its own: an addon that keeps part of an
# instance's copy beside the duplicity prefix (continuous recovery's
# ``<name>-pitr``) hands it here, with its own ``DST`` and the same
# bounds. They run in order and the first that fails stops the
# others, with its exit code: the record stays, and the next run finds
# the ones before it empty. The result is 0 when any of them removed
# something, 10 when every one was already empty. All of them must have
# arrived before anything is deleted.
#
# An SFTP copy (``DST=sftp://…``) also needs the destination's key and
# trusted server key, uploaded beside the first file as ``purge-ssh-key``
# and ``purge-known_hosts``. They are mounted read-only where the program
# reads them, with ``--mount``, which fails on a missing file instead of
# creating a directory in its place, and removed with the rest. A
# destination that logs in with a password sends no key: its environment
# files carry ``BACKEND_PASSWORD``, and only the server key is mounted.
#
# Exit codes are the contract with the executor:
#
#   0   purged — the prefix is empty and was verified empty
#   10  nothing to purge — the prefix was already empty
#   20  the environment file (or an SFTP copy's key files) is missing
#   21  the provider rejected the credentials
#   22  the container could not start, or any other failure
#   23  an object stored since the cutoff continues a chain the purge
#       would delete (``lib/purge_prefix.sh``): nothing deleted under
#       that prefix
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"
# shellcheck source-path=SCRIPTDIR source=lib/purge_prefix.sh
source "$(dirname "$0")/lib/purge_prefix.sh"

ic_require_args 2 "$#" "backup_purge_archived.sh <env_file> <image> [<env_file>...]"

env_file="$1"
image="$2"
shift 2
env_files=("$env_file" "$@")
ssh_key="$(dirname "$env_file")/purge-ssh-key"
known_hosts="$(dirname "$env_file")/purge-known_hosts"

# Removed whatever happens: they hold the destination's credentials, and
# a failed run must not leave them on the host's /tmp. The directory
# goes too, with a plain rmdir — it is empty by then, and a recursive
# delete of a path that arrived as an argument is not a thing this
# script should be able to do.
trap 'rm -f "${env_files[@]}" "$ssh_key" "$known_hosts"; rmdir "$(dirname "$env_file")" 2>/dev/null || true' EXIT

for file in "${env_files[@]}"; do
    if [ ! -f "$file" ]; then
        ic_warn "environment file not found: $file"
        exit 20
    fi
done

ssh_mounts=()
if grep -q '^DST=sftp://' "${env_files[@]}"; then
    ssh_files=("$known_hosts")
    if ! grep -q '^BACKEND_PASSWORD=' "${env_files[@]}"; then
        ssh_files+=("$ssh_key")
        ssh_mounts=(--mount "type=bind,src=$ssh_key,dst=/root/.ssh/id_ed25519,readonly")
    fi
    for file in "${ssh_files[@]}"; do
        if [ ! -f "$file" ]; then
            ic_warn "SFTP key file not found: $file"
            exit 20
        fi
    done
    ssh_mounts+=(
        --mount "type=bind,src=$known_hosts,dst=/root/.ssh/known_hosts,readonly"
    )
fi

ic_log "purging archived backup prefix via an ephemeral container"

rc=10
pass=0
for file in "${env_files[@]}"; do
    pass=$((pass + 1))
    if [ "${#env_files[@]}" -gt 1 ]; then
        ic_log "prefix ${pass} of ${#env_files[@]}"
    fi
    set +e
    ic_purge_prefix_program \
        | docker run --rm -i --env-file "$file" "${ssh_mounts[@]}" \
            --entrypoint python3 "$image" -
    one=$?
    set -e
    case "$one" in
        0)  rc=0 ;;
        10) ;;
        *)  rc="$one"; break ;;
    esac
done

case "$rc" in
    0)  ic_log "archived backup prefix purged." ;;
    10) ic_log "archived backup prefix was already empty." ;;
    21) ic_warn "storage rejected the credentials." ;;
    20) ic_warn "environment unavailable." ;;
    23) ic_warn "the instance that took the name continued this chain: nothing deleted." ;;
    *)  ic_warn "archived backup purge failed (exit $rc)."
        # Anything the inner program did not classify — most often the
        # image failing to pull or to start at all — lands here as 22.
        rc=22 ;;
esac

exit "$rc"
