#!/usr/bin/env bats
# Tests for scripts/backup_purge.sh and scripts/backup_purge_archived.sh.
#
# ``docker`` is stubbed on PATH, so what gets exercised is the shell's
# own contract: which container it builds, that the deletion program is
# fed on stdin rather than baked into an argument, and — the part that
# actually matters — that every exit code the executors classify on is
# passed through unchanged. A purge that returns 22 where it meant 21
# sends an operator to look at the wrong thing.
#
# The two scripts share the deletion program (lib/purge_prefix.sh), so
# the last test here checks they really do: a copy that drifts would
# only ever show up as objects surviving a deletion, which nothing
# notices.

setup() {
    SCRIPTS="${BATS_TEST_DIRNAME}/../../scripts"
    PURGE="$SCRIPTS/backup_purge.sh"
    ARCHIVED_PURGE="$SCRIPTS/backup_purge_archived.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    mkdir -p "$TMP/bin"
    export DOCKER_CALLS="$TMP/calls"
    export DOCKER_STDIN="$TMP/stdin"
    export DOCKER_EXIT="$TMP/exit"
    : > "$DOCKER_CALLS"
    : > "$DOCKER_STDIN"
    echo 0 > "$DOCKER_EXIT"

    # Stub docker: record argv and whatever was piped in, then exit with
    # whatever the test asked for. ``compose config --services`` answers
    # the service list so the purge's own drift check passes.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
if [ "$1" = "compose" ] && [ "$2" = "config" ]; then
    printf 'odoo\ndb\nbackup\n'
    exit 0
fi
cat >> "$DOCKER_STDIN"
exit "$(cat "$DOCKER_EXIT")"
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

_env_file() {
    printf 'DST=boto3+s3://bucket/path/proj/inst\n' > "$TMP/purge.env"
    printf '%s\n' "$TMP/purge.env"
}

# ── backup_purge.sh: the instance's own container ────────────────────

@test "purge runs the backup service with compose run, not exec" {
    # ``exec`` needs the stack up; a stopped instance must purge too.
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    grep -q 'docker compose run --rm -T --entrypoint python3 backup -' \
        "$DOCKER_CALLS"
    ! grep -q 'compose exec' "$DOCKER_CALLS"
}

@test "purge feeds the deletion program on stdin" {
    run bash "$PURGE" "$HOME/project/inst"
    grep -q 'IC_PURGE_OK' "$DOCKER_STDIN"
    grep -q 'refusing to purge without a per-instance prefix' "$DOCKER_STDIN"
}

@test "purge accepts a tilde path" {
    run bash "$PURGE" "~/project/inst"
    [ "$status" -eq 0 ]
}

@test "purge reports drift when the directory is gone" {
    run bash "$PURGE" "$HOME/project/gone"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "purge reports drift when there is no backup service" {
    # ``compose exec`` answers "not running" for a stopped service and
    # for one that does not exist, so this is checked explicitly.
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
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 20 ]
    [[ "$output" == *"no 'backup' service"* ]]
}

@test "purge passes an already-empty prefix through as 10" {
    # Not a failure: the invariant already holds and the caller proceeds.
    echo 10 > "$DOCKER_EXIT"
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 10 ]
}

@test "purge passes a credentials rejection through as 21" {
    echo 21 > "$DOCKER_EXIT"
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 21 ]
}

@test "purge collapses an unclassified failure to 22" {
    # A container that cannot start at all lands here; the executor's
    # catch-all alert is what the operator gets.
    echo 137 > "$DOCKER_EXIT"
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 22 ]
}

# ── backup_purge_archived.sh: the ephemeral container ────────────────

@test "archived purge runs a throwaway container, not compose" {
    env_file="$(_env_file)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 0 ]
    grep -q 'docker run --rm -i --env-file' "$DOCKER_CALLS"
    grep -q 'some/image:tag' "$DOCKER_CALLS"
    ! grep -q 'compose' "$DOCKER_CALLS"
}

@test "archived purge never puts the environment in an argument" {
    # An argument is visible in ps to every account on the host for as
    # long as the container runs.
    env_file="$(_env_file)"
    printf 'AWS_SECRET_ACCESS_KEY=hunter2\n' >> "$env_file"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    ! grep -q 'hunter2' "$DOCKER_CALLS"
}

@test "archived purge deletes the environment file afterwards" {
    env_file="$(_env_file)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ ! -f "$env_file" ]
}

@test "archived purge deletes the environment file even when it fails" {
    env_file="$(_env_file)"
    echo 22 > "$DOCKER_EXIT"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 22 ]
    [ ! -f "$env_file" ]
}

@test "archived purge reports drift when the environment never arrived" {
    run bash "$ARCHIVED_PURGE" "$TMP/never-uploaded.env" "some/image:tag"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "archived purge passes an already-empty prefix through as 10" {
    env_file="$(_env_file)"
    echo 10 > "$DOCKER_EXIT"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 10 ]
}

@test "archived purge passes a credentials rejection through as 21" {
    env_file="$(_env_file)"
    echo 21 > "$DOCKER_EXIT"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 21 ]
}

@test "archived purge collapses a failed image pull to 22" {
    env_file="$(_env_file)"
    echo 125 > "$DOCKER_EXIT"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 22 ]
}

@test "archived purge refuses to run without both arguments" {
    run bash "$ARCHIVED_PURGE" "$TMP/purge.env"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage: backup_purge_archived.sh"* ]]
}

# ── The two must delete the same way ─────────────────────────────────

@test "both purges send byte-for-byte the same program" {
    # They serve one invariant — no objects belonging to an instance
    # that no longer exists — from two different containers. Two copies
    # of the deletion would be two chances to fix a bug in one only.
    run bash "$PURGE" "$HOME/project/inst"
    cp "$DOCKER_STDIN" "$TMP/from-compose"
    : > "$DOCKER_STDIN"
    env_file="$(_env_file)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    diff "$TMP/from-compose" "$DOCKER_STDIN"
}
