#!/usr/bin/env bash
# Take the archive copy of an instance and prune to that copy alone.
#
# Usage: backup_archive.sh <instance_dir>
#
# Archiving keeps exactly one restorable copy, taken at the moment of
# archiving rather than whenever the last nightly happened — otherwise
# the delta since then is lost silently.
#
# Order and placement both matter. In duplicity the restorable unit is
# the *chain* (a full plus the incrementals that depend on it), not "a
# copy", so "keep the last one" means: take a fresh full, then
# ``remove-all-but-n-full 1``, which leaves that full as the only chain.
# Doing it the other way round would prune to a chain and then add a
# second one. And all of it has to happen before the teardown, because
# ``compose down -v`` destroys the container that holds duplicity, the
# credentials and the passphrase.
#
# ``run --rm`` rather than ``exec``: it builds the container from the
# compose definition without needing the stack up, so an instance that
# is stopped archives just as well as a running one. Through the
# image's own entrypoint, never ``--entrypoint sh``: the postgres image
# installs the client matching its ``DB_VERSION`` when a container
# starts, so a container that skips it has no ``psql`` to dump with
# (10-oct-2026: every archive in production had failed so).
#
# ``run`` also starts what ``backup`` depends on (``db``, ``smtp``).
# When the archive fails the instance stays, so it is left as it was
# found: whatever the step started is stopped again. Only when what was
# running before could be read: guessing could stop a live instance.
#
# Exit codes are the contract with the executor, which classifies the
# failure from the code alone — never by matching duplicity's wording,
# which would rot the day the image changes version:
#
#   0   archived — a fresh full exists and it is the only chain
#   20  no ``backup`` service in this compose (panel/host drift)
#   22  anything else: the container could not start, the dump failed,
#       duplicity refused, the provider rejected the credentials
#   23  the compose could not be read at all (docker's error is in the
#       job log) — unlike 20, this says nothing about what it declares
#
# Deliberately coarser than backup_purge.sh, which distinguishes an
# auth failure. There the S3 call is ours and the error code is
# structured; here duplicity is in the middle and its exit codes do not
# separate "wrong key" from "cannot read the chain" in a way worth
# pretending to. A code we cannot tell apart honestly is better reported
# as unknown, with the container's own output in the job log, than
# guessed at.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "backup_archive.sh <instance_dir>"

dir="$(ic_expand_home "$1")"

if [ ! -d "$dir" ]; then
    ic_warn "instance directory not found: $dir"
    exit 20
fi

cd "$dir"

if ! services="$(ic_compose_services)"; then
    # Not drift: nothing is known about what the compose declares, only
    # that the host could not read it. The helper has already put
    # docker's own error in the log.
    exit 23
fi

if ! printf '%s\n' "$services" | grep -qx backup; then
    # Checked here rather than inferred from a failed exec: ``docker
    # compose exec`` answers `service "backup" is not running` both when
    # the service is stopped and when it does not exist, so that message
    # can never tell the two apart.
    ic_warn "no 'backup' service declared in $dir"
    exit 20
fi

ic_log "taking the archive copy and pruning to it"

# What was running before the step, to leave the instance as it was if
# the archive fails. Empty and unknown are different answers.
if running_before="$(docker compose ps --services --status running 2>/dev/null)"; then
    known_before=1
else
    known_before=0
fi

# Stop what the archive step started and was not running before.
#
# Called only when the archive failed: on success the teardown that
# follows removes everything anyway. Never fails the script; the exit
# code stays the archive's.
stop_what_the_step_started() {
    local now svc
    local started=()
    if [ "$known_before" != 1 ]; then
        ic_warn "could not read what was running before; leaving the stack as it is."
        return 0
    fi
    if ! now="$(docker compose ps --services --status running 2>/dev/null)"; then
        ic_warn "could not read what is running now; leaving the stack as it is."
        return 0
    fi
    while IFS= read -r svc; do
        [ -n "$svc" ] || continue
        printf '%s\n' "$running_before" | grep -qx "$svc" || started+=("$svc")
    done <<< "$now"
    if [ "${#started[@]}" -gt 0 ]; then
        ic_log "stopping what the archive step started: ${started[*]}"
        docker compose stop "${started[@]}" \
            || ic_warn "could not stop ${started[*]}."
    fi
}

set +e
docker compose run --rm -T backup sh -c '
set -eu
# On a stopped instance ``run`` starts the database with this container,
# and on a loaded host it may not answer yet when the dump starts: wait
# for it, a minute at most. By the clock, not by attempts: an attempt
# can take seconds of its own (60 of them took six minutes when the
# name did not resolve).
deadline=$(($(date +%s) + 60))
until pg_isready -q -t 5; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "ERROR the database did not answer within 60s" >&2
        exit 22
    fi
    sleep 1
done
# Dump the databases into $SRC first: ``dup full`` uploads whatever is
# in $SRC, so without this the archive copy would be whatever the last
# nightly left there. Reuse the JOB_200 command the image itself
# declares rather than repeating its pg_dump invocation, so the two
# cannot drift apart.
if [ -n "${JOB_200_WHAT:-}" ]; then
    eval "$JOB_200_WHAT"
else
    echo "ERROR the image declares no JOB_200_WHAT to dump with" >&2
    exit 22
fi
dup full "$SRC" "$DST"
# n=1 keeps exactly the full just taken. Anything older, full or
# incremental, goes: after the teardown nothing is left to prune it.
dup --force remove-all-but-n-full 1 "$DST"
dup --force cleanup "$DST"
' 2>&1
rc=$?
set -e

case "$rc" in
    0)  ic_log "archive copy taken; older chains pruned." ;;
    *)  # duplicity exits 30-ish on auth problems depending on backend;
        # anything we cannot attribute is 22 and the operator reads the
        # job log, which carries the container output above.
        ic_warn "archive step failed (exit $rc)."
        stop_what_the_step_started
        rc=22 ;;
esac

exit "$rc"
