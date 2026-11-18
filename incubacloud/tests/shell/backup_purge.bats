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
    export DOCKER_EXITS="$TMP/exits"

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
# ``$DOCKER_EXITS``, when a test writes it, answers call by call: its
# first line is consumed by this call. Otherwise ``$DOCKER_EXIT``.
if [ -s "${DOCKER_EXITS:-}" ]; then
    head -n 1 "$DOCKER_EXITS" > "$DOCKER_EXITS.this"
    tail -n +2 "$DOCKER_EXITS" > "$DOCKER_EXITS.rest"
    mv "$DOCKER_EXITS.rest" "$DOCKER_EXITS"
    exit "$(cat "$DOCKER_EXITS.this")"
fi
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
    grep -q 'docker compose run --label traefik.enable=false --label sablier.enable=false --rm -T --no-deps --entrypoint python3 backup -' \
        "$DOCKER_CALLS"
    ! grep -q 'compose exec' "$DOCKER_CALLS"
}

@test "purge starts nothing but its own container" {
    # It talks to the bucket alone, with backup.env: neither ``db`` nor
    # ``smtp`` is any of its business. Without ``--no-deps`` Compose
    # starts the backup service's depends_on and recreates every one
    # whose definition changed since the stack started: a live instance
    # with a rewritten compose would get its ``db`` recreated under its
    # workers, and keep it so if the purge then failed.
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'compose run' "$DOCKER_CALLS")" -eq 1 ]
    grep 'compose run' "$DOCKER_CALLS" | grep -q -- ' --no-deps '
    ! grep -Eq 'compose (start|up|restart)' "$DOCKER_CALLS"
}

@test "purge bounds the container to the since it is given, and only then" {
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    ! grep -q 'PURGE_SINCE' "$DOCKER_CALLS"
    : > "$DOCKER_CALLS"
    run bash "$PURGE" "$HOME/project/inst" "2026-02-01T00:00:00+00:00"
    [ "$status" -eq 0 ]
    grep 'compose run' "$DOCKER_CALLS" \
        | grep -q -- ' --no-deps -e PURGE_SINCE=2026-02-01T00:00:00+00:00 --entrypoint python3 backup -'
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
    # Retrying would be wrong here: the command answered, and the answer
    # was "no such service".
    [ "$(grep -c 'compose config' "$DOCKER_CALLS")" -eq 1 ]
}

@test "purge reports an unreadable compose as 23, not as drift" {
    # Production, 2026-09-19: ``compose config`` died in under 110 ms
    # and the purge reported "no backup service declared" — a
    # permanent-drift diagnosis whose advice is "rebuild" — with the
    # reason gone to /dev/null. A failed read must say so, keep
    # docker's error, and never reach the container.
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
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 23 ]
    [[ "$output" == *"could not read the compose"* ]]
    [[ "$output" == *"no configuration file provided"* ]]
    [[ "$output" != *"no 'backup' service"* ]]
    ! grep -q 'compose run' "$DOCKER_CALLS"
}

@test "purge retries a failed compose read once and says so" {
    # The read has no side effects, so a second attempt costs two
    # seconds on the host rather than a failed job. The log line is
    # what keeps a recurring transient from being invisible.
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
cat > /dev/null
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$PURGE" "$HOME/project/inst"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'compose config' "$DOCKER_CALLS")" -eq 2 ]
    [[ "$output" == *"retrying once"* ]]
    grep -q 'compose run' "$DOCKER_CALLS"
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

@test "archived purge passes a chain the successor continued through as 23" {
    env_file="$(_env_file)"
    echo 23 > "$DOCKER_EXIT"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 23 ]
    [[ "$output" == *"continued this chain: nothing deleted"* ]]
}

@test "archived purge stops at a prefix whose chain was continued" {
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '23\n0\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 23 ]
    [ "$(grep -c 'docker run' "$DOCKER_CALLS")" -eq 1 ]
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

# ── backup_purge_archived.sh: further prefixes of the same copy ──────

# A further environment file, for one more prefix of the copy.
_more_env_file() {
    printf 'DST=s3://bucket/path/proj/inst-%s\n' "$1" > "$TMP/purge-$1.env"
    printf '%s\n' "$TMP/purge-$1.env"
}

@test "archived purge empties every prefix it is handed, each in its own container" {
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'docker run' "$DOCKER_CALLS")" -eq 2 ]
    grep -q -- "--env-file $env_file " "$DOCKER_CALLS"
    grep -q -- "--env-file $more " "$DOCKER_CALLS"
}

@test "archived purge stops at the first prefix that fails, with its code" {
    # The record stays for the next run; what is left goes then.
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '21\n0\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 21 ]
    [ "$(grep -c 'docker run' "$DOCKER_CALLS")" -eq 1 ]
}

@test "archived purge fails when a further prefix fails after the first emptied" {
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '0\n125\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 22 ]
}

@test "archived purge is already empty only when every prefix was" {
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '10\n0\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 0 ]
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '10\n10\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 10 ]
}

@test "archived purge deletes every environment file, failure included" {
    env_file="$(_env_file)"
    more="$(_more_env_file pitr)"
    printf '0\n22\n' > "$DOCKER_EXITS"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$more"
    [ "$status" -eq 22 ]
    [ ! -f "$env_file" ]
    [ ! -f "$more" ]
}

@test "archived purge deletes nothing when a further environment never arrived" {
    env_file="$(_env_file)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag" "$TMP/never.env"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
    [ ! -f "$env_file" ]
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

# ── backup_purge_archived.sh: an SFTP copy ───────────────────────────

_sftp_env_file() {
    env_file="$(_env_file)"
    printf 'DST=sftp://u1@backup.example.com/backups/proj/inst\n' > "$env_file"
    printf 'KEY\n' > "$(dirname "$env_file")/purge-ssh-key"
    printf 'backup.example.com ssh-ed25519 AAAA\n' > "$(dirname "$env_file")/purge-known_hosts"
    echo "$env_file"
}

@test "archived purge mounts an SFTP copy's key files read-only, then removes them" {
    env_file="$(_sftp_env_file)"
    dir="$(dirname "$env_file")"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 0 ]
    grep -q -- "--mount type=bind,src=$dir/purge-ssh-key,dst=/root/.ssh/id_ed25519,readonly" "$DOCKER_CALLS"
    grep -q -- "--mount type=bind,src=$dir/purge-known_hosts,dst=/root/.ssh/known_hosts,readonly" "$DOCKER_CALLS"
    ! grep -q 'KEY' "$DOCKER_CALLS"
    [ ! -f "$dir/purge-ssh-key" ]
    [ ! -f "$dir/purge-known_hosts" ]
}

@test "archived purge of an SFTP copy without its key files is 20, nothing runs" {
    env_file="$(_sftp_env_file)"
    rm "$(dirname "$env_file")/purge-known_hosts"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
    [ ! -f "$(dirname "$env_file")/purge-ssh-key" ]
}

@test "archived purge of an SFTP copy without its key is 20, nothing runs" {
    env_file="$(_sftp_env_file)"
    rm "$(dirname "$env_file")/purge-ssh-key"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 20 ]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "archived purge of an SFTP copy with a password mounts the trusted key only" {
    # The password is a line of the environment file; no key file is sent.
    env_file="$(_sftp_env_file)"
    dir="$(dirname "$env_file")"
    rm "$dir/purge-ssh-key"
    printf 'BACKEND_PASSWORD=Correct-horse 9 battery!\n' >> "$env_file"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    [ "$status" -eq 0 ]
    grep -q -- "--mount type=bind,src=$dir/purge-known_hosts,dst=/root/.ssh/known_hosts,readonly" "$DOCKER_CALLS"
    ! grep -q -- "id_ed25519" "$DOCKER_CALLS"
    ! grep -q 'Correct-horse' "$DOCKER_CALLS"
    [ ! -f "$env_file" ]
    [ ! -f "$dir/purge-known_hosts" ]
}

@test "archived purge of an S3 copy mounts nothing" {
    env_file="$(_env_file)"
    run bash "$ARCHIVED_PURGE" "$env_file" "some/image:tag"
    ! grep -q -- '--mount' "$DOCKER_CALLS"
}
