#!/usr/bin/env bats
# Tests for scripts/backup_archive.sh.
#
# ``docker`` is stubbed on PATH and the inner shell script it would run
# is captured instead of executed, so what is exercised is the contract:
# a fresh full first and the prune second, the image's own dump command
# reused rather than repeated, and the exit codes the executor
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
    : > "$DOCKER_CALLS"
    : > "$INNER"
    echo 0 > "$DOCKER_EXIT"

    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    printf 'odoo\ndb\nbackup\n'
    exit 0
fi
# The last argument of ``compose run ... sh -c <program>`` is the
# program the container would have run; keep it for inspection.
for arg in "$@"; do :; done
printf '%s' "$arg" > "$INNER"
exit "$(cat "$DOCKER_EXIT")"
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
    grep -q 'docker compose run --rm -T --entrypoint sh backup -c' \
        "$DOCKER_CALLS"
    ! grep -q 'compose exec' "$DOCKER_CALLS"
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
