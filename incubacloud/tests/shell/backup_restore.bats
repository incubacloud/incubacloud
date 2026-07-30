#!/usr/bin/env bats
# Tests for scripts/backup_restore.sh — the container-side steps of the
# production restore flow. ``docker`` is stubbed to record its argv.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/backup_restore.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"

    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/docker"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "restore drives duplicity in the backup container" {
    run bash "$SCRIPT" restore "$DIR" 2026-03-19T02:00:00
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup sh -c"* ]]
    [[ "$(cat "$CALLS")" == *"dup restore --time \"2026-03-19T02:00:00\" --force"* ]]
}

@test "restore leaves \$DST and \$SRC for the container to expand" {
    run bash "$SCRIPT" restore "$DIR" latest
    [[ "$(cat "$CALLS")" == *'"$DST"'* ]]
    [[ "$(cat "$CALLS")" == *'"$SRC"'* ]]
}

@test "dropdb tolerates an absent database" {
    run bash "$SCRIPT" dropdb "$DIR" prod
    [ "$status" -eq 0 ]
    [ "$(cat "$CALLS")" = "docker compose exec -T backup dropdb --if-exists prod" ]
}

@test "createdb creates the named database" {
    run bash "$SCRIPT" createdb "$DIR" prod
    [ "$(cat "$CALLS")" = "docker compose exec -T backup createdb prod" ]
}

@test "import-sql fails on the first error, not silently" {
    # psql returns 0 on a partial import unless ON_ERROR_STOP is set,
    # which would report a broken restore as success.
    run bash "$SCRIPT" import-sql "$DIR" prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"psql -v ON_ERROR_STOP=1 -d prod -f \$SRC/prod.sql"* ]]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" restore "$HOME/projects/gone" latest
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" arg
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}
