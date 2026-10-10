#!/usr/bin/env bats
# Tests for scripts/backup_archive.sh.
#
# ``docker`` is stubbed on PATH and the inner shell script it would run
# is captured instead of executed, so what is exercised is the contract:
# a fresh full first and the prune second, the image's own dump command
# and entrypoint reused rather than repeated or skipped, a failed archive
# leaving the instance as it found it, and the exit codes the executor
# classifies on.
#
# The order is the whole design and it is easy to get backwards. In
# duplicity the restorable unit is the chain, so "keep the last copy"
# means take a full and *then* prune to one — pruning first and
# uploading after leaves two chains, which is exactly what archiving
# promised not to do.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/backup_archive.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    mkdir -p "$TMP/bin"
    export DOCKER_CALLS="$TMP/calls"
    export DOCKER_EXIT="$TMP/exit"
    export INNER="$TMP/inner"
    # What ``compose ps --status running`` answers before and after the
    # ``run``; PS_FAIL makes it fail before the run.
    export PS_BEFORE="$TMP/ps_before"
    export PS_AFTER="$TMP/ps_after"
    export PS_FAIL="$TMP/ps_fail"
    export RAN="$TMP/ran"
    : > "$DOCKER_CALLS"
    : > "$INNER"
    : > "$PS_BEFORE"
    : > "$PS_AFTER"
    echo 0 > "$DOCKER_EXIT"

    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    printf 'odoo\ndb\nbackup\n'
    exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
    if [ -e "$RAN" ]; then
        cat "$PS_AFTER"
    else
        [ ! -e "$PS_FAIL" ] || exit 1
        cat "$PS_BEFORE"
    fi
    exit 0
fi
if [ "$1" = "compose" ] && [ "$2" = "run" ]; then
    touch "$RAN"
    # The last argument of ``compose run ... sh -c <program>`` is the
    # program the container would have run; keep it for inspection.
    for arg in "$@"; do :; done
    printf '%s' "$arg" > "$INNER"
    exit "$(cat "$DOCKER_EXIT")"
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "archive runs the backup service with compose run, not exec" {
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    grep -q 'docker compose run --rm -T backup sh -c' "$DOCKER_CALLS"
    ! grep -q 'compose exec' "$DOCKER_CALLS"
}

@test "the container goes through the image's own entrypoint" {
    # The entrypoint installs the postgres client matching DB_VERSION;
    # skipping it left no psql, and every archive in production failed
    # with "psql: not found" (10-oct-2026).
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    ! grep -q -- '--entrypoint' "$DOCKER_CALLS"
}

@test "a failed archive stops what it started" {
    # A suspended instance: nothing running, and the run brought up the
    # database and the mail relay the backup service depends on.
    printf 'db\nsmtp\n' > "$PS_AFTER"
    echo 30 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
    grep -qx 'docker compose stop db smtp' "$DOCKER_CALLS"
    [[ "$output" == *"stopping what the archive step started: db smtp"* ]]
}

@test "a failed archive leaves running what was already running" {
    printf 'db\n' > "$PS_BEFORE"
    printf 'db\nsmtp\n' > "$PS_AFTER"
    echo 30 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
    grep -qx 'docker compose stop smtp' "$DOCKER_CALLS"
}

@test "a failed archive on a live instance stops nothing" {
    printf 'odoo\ndb\nsmtp\nbackup\n' > "$PS_BEFORE"
    printf 'odoo\ndb\nsmtp\nbackup\n' > "$PS_AFTER"
    echo 30 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
    ! grep -q 'compose stop' "$DOCKER_CALLS"
}

@test "nothing is stopped when what was running could not be read" {
    # Empty and unknown are different answers: guessing "nothing was
    # running" could stop a live instance.
    touch "$PS_FAIL"
    printf 'odoo\ndb\nsmtp\n' > "$PS_AFTER"
    echo 30 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
    ! grep -q 'compose stop' "$DOCKER_CALLS"
    [[ "$output" == *"leaving the stack as it is"* ]]
}

@test "a successful archive stops nothing: the teardown follows" {
    printf 'db\nsmtp\n' > "$PS_AFTER"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    ! grep -q 'compose stop' "$DOCKER_CALLS"
}

@test "the full is taken before anything is pruned" {
    # Backwards, this leaves two chains: the prune would keep the old
    # full and the fresh one would be added after it.
    run bash "$SCRIPT" "$HOME/project/inst"
    full_line="$(grep -n 'dup full' "$INNER" | head -1 | cut -d: -f1)"
    prune_line="$(grep -n 'remove-all-but-n-full' "$INNER" | head -1 | cut -d: -f1)"
    [ -n "$full_line" ]
    [ -n "$prune_line" ]
    [ "$full_line" -lt "$prune_line" ]
}

@test "the prune keeps exactly one full" {
    run bash "$SCRIPT" "$HOME/project/inst"
    grep -q 'remove-all-but-n-full 1' "$INNER"
}

@test "the databases are dumped with the image's own command" {
    # Repeating the pg_dump invocation here would let the two drift the
    # day the image changes it.
    run bash "$SCRIPT" "$HOME/project/inst"
    grep -q 'JOB_200_WHAT' "$INNER"
    grep -q 'eval "\$JOB_200_WHAT"' "$INNER"
}

@test "the dump waits for the database, a minute at most" {
    # On a stopped instance the run starts the database with it; on a
    # loaded host it may not answer yet when the dump starts.
    run bash "$SCRIPT" "$HOME/project/inst"
    wait_line="$(grep -n 'until pg_isready' "$INNER" | head -1 | cut -d: -f1)"
    dump_line="$(grep -n 'eval "\$JOB_200_WHAT"' "$INNER" | head -1 | cut -d: -f1)"
    [ -n "$wait_line" ]
    [ -n "$dump_line" ]
    [ "$wait_line" -lt "$dump_line" ]
    # Bounded by the clock: one attempt alone can take seconds.
    grep -q 'deadline=\$((\$(date +%s) + 60))' "$INNER"
    grep -q 'pg_isready -q -t 5' "$INNER"
}

@test "the copy uploads what was just dumped" {
    run bash "$SCRIPT" "$HOME/project/inst"
    grep -q 'dup full "\$SRC" "\$DST"' "$INNER"
}

@test "orphaned duplicity metadata is cleaned up after the prune" {
    run bash "$SCRIPT" "$HOME/project/inst"
    grep -q 'cleanup' "$INNER"
}

@test "archive accepts a tilde path" {
    run bash "$SCRIPT" "~/project/inst"
    [ "$status" -eq 0 ]
}

@test "archive reports drift when the directory is gone" {
    run bash "$SCRIPT" "$HOME/project/gone"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "archive reports drift when there is no backup service" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    printf 'odoo\ndb\n'
    exit 0
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 20 ]
    [[ "$output" == *"no 'backup' service"* ]]
    # The command answered; retrying an answer would be wrong.
    [ "$(grep -c 'compose config' "$DOCKER_CALLS")" -eq 1 ]
}

@test "archive reports an unreadable compose as 23, not as drift" {
    # Same contract as the purge: a read that failed says nothing about
    # what the compose declares, so it must not be reported as drift
    # (whose advice is "rebuild"), and docker's error must reach the log.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    echo "no configuration file provided: not found" >&2
    exit 1
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 23 ]
    [[ "$output" == *"could not read the compose"* ]]
    [[ "$output" == *"no configuration file provided"* ]]
    [[ "$output" != *"no 'backup' service"* ]]
    ! grep -q 'compose run' "$DOCKER_CALLS"
}

@test "archive retries a failed compose read once and says so" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    if [ "$(grep -c 'compose config' "$DOCKER_CALLS")" -eq 1 ]; then
        echo "transient" >&2
        exit 1
    fi
    printf 'odoo\ndb\nbackup\n'
    exit 0
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'compose config' "$DOCKER_CALLS")" -eq 2 ]
    [[ "$output" == *"retrying once"* ]]
    grep -q 'compose run' "$DOCKER_CALLS"
}

@test "any failure the script cannot attribute is reported as 22" {
    # duplicity's own codes do not separate a wrong key from an
    # unreadable chain in a way worth pretending to, so the operator
    # gets the container's output in the job log instead of a guess.
    echo 30 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
}

@test "a container that cannot start is also 22" {
    echo 125 > "$DOCKER_EXIT"
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 22 ]
}

@test "archive refuses to run without an instance directory" {
    run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage: backup_archive.sh"* ]]
}
