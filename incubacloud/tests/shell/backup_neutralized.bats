#!/usr/bin/env bats
# Tests for scripts/backup_neutralized.sh.
#
# ``docker`` and ``zip`` are stubbed on PATH, so what is exercised here
# is the shell logic that used to live inside the executor's f-strings:
# which container each step talks to, the duplicity flags, the
# neutralize flags, the pg_dump branch and the root cleanup.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/backup_neutralized.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"
    HOST_TMP="$TMP/hosttmp"

    mkdir -p "$TMP/bin"
    for tool in docker zip; do
        cat > "$TMP/bin/$tool" <<STUB
#!/usr/bin/env bash
echo "$tool \$*" >> "\$CALLS"
STUB
        chmod +x "$TMP/bin/$tool"
    done
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "prod source restore drives duplicity in the backup container" {
    run bash "$SCRIPT" prepare-src-prod "$DIR" 42 prod latest "$HOST_TMP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup sh -c"* ]]
    [[ "$(cat "$CALLS")" == *"dup restore --force"* ]]
    [[ "$(cat "$CALLS")" == *"--path-to-restore prod.sql"* ]]
}

@test "prod source restore leaves \$DST for the container to expand" {
    run bash "$SCRIPT" prepare-src-prod "$DIR" 42 prod latest "$HOST_TMP"
    [[ "$(cat "$CALLS")" == *'"$DST"'* ]]
}

@test "'latest' means no --time flag" {
    run bash "$SCRIPT" prepare-src-prod "$DIR" 42 prod latest "$HOST_TMP"
    [[ "$(cat "$CALLS")" != *"--time"* ]]
}

@test "an explicit timestamp becomes a --time flag" {
    run bash "$SCRIPT" prepare-src-prod "$DIR" 42 prod 12h_ago "$HOST_TMP"
    [[ "$(cat "$CALLS")" == *"--time 12h_ago"* ]]
}

@test "prod repackages the dump as a zip the odoo container can restore" {
    run bash "$SCRIPT" prepare-src-prod "$DIR" 42 prod latest "$HOST_TMP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"zip -q -r src.zip dump.sql"* ]]
    [[ "$(cat "$CALLS")" == *"odoo:/tmp/bkneu-src-42.zip"* ]]
}

@test "non-prod dumps the live database instead" {
    run bash "$SCRIPT" prepare-src-live "$DIR" 42 prod "$HOST_TMP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T odoo click-odoo-backupdb prod /tmp/bkneu-src-42.zip"* ]]
    [[ "$(cat "$CALLS")" != *"dup restore"* ]]
    [ -d "$HOST_TMP" ]
}

@test "the neutralize step forces a neutralized restore" {
    run bash "$SCRIPT" restore-neutralize "$DIR" 42 __ic_neutral_42
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"click-odoo-restoredb --neutralize --force __ic_neutral_42 /tmp/bkneu-src-42.zip"* ]]
}

@test "the filestore redump uses click-odoo-backupdb" {
    run bash "$SCRIPT" redump-full "$DIR" 42 __ic_neutral_42 /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"click-odoo-backupdb __ic_neutral_42 /tmp/bkneu-out-42.zip"* ]]
    [[ "$(cat "$CALLS")" == *"docker compose cp odoo:/tmp/bkneu-out-42.zip /tmp/out.zip"* ]]
}

@test "the SQL-only redump uses pg_dump and zips it" {
    mkdir -p "$HOST_TMP"
    run bash "$SCRIPT" redump-sql "$DIR" 42 __ic_neutral_42 "$HOST_TMP" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"pg_dump --no-owner --no-privileges --dbname=__ic_neutral_42"* ]]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql"* ]]
    [[ "$(cat "$CALLS")" != *"click-odoo-backupdb"* ]]
}

@test "cleanup drops the temp DB as root and removes the host scratch dir" {
    mkdir -p "$HOST_TMP"
    run bash "$SCRIPT" cleanup "$DIR" 42 __ic_neutral_42 "$HOST_TMP"
    [ "$status" -eq 0 ]
    # docker compose cp lands the source ZIP owned by root, so the
    # cleanup rm needs --user root or it fails with EPERM.
    [[ "$(cat "$CALLS")" == *"docker compose exec --user root -T odoo sh -c"* ]]
    [[ "$(cat "$CALLS")" == *"dropdb --if-exists __ic_neutral_42"* ]]
    [[ "$(cat "$CALLS")" == *"rm -rf /var/lib/odoo/filestore/__ic_neutral_42"* ]]
    [ ! -d "$HOST_TMP" ]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" restore-neutralize "~/projects/inst" 42 __ic_neutral_42
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" restore-neutralize "$HOME/projects/gone" 42 db
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" 42
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}
