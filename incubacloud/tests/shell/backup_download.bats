#!/usr/bin/env bats
# Tests for scripts/backup_download.sh.
#
# ``docker`` and ``zip`` are stubbed on PATH. What is exercised here is
# the logic that used to live in the executor's f-strings: which
# container each mode talks to, the duplicity flags, and the packaging.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/backup_download.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"
    TMPDIR_ARG="$TMP/scratch"

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

@test "a db-only live dump asks for no filestore" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip db
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --rm -v /tmp:/host-tmp odoo click-odoo-backupdb --no-filestore prod /host-tmp/out.zip"* ]]
}

@test "an 'all' live dump includes the filestore" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip all
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"--filestore prod"* ]]
    [[ "$(cat "$CALLS")" != *"--no-filestore"* ]]
}

@test "a live dump never touches the backup container" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip db
    [[ "$(cat "$CALLS")" != *"exec -T backup"* ]]
    [[ "$(cat "$CALLS")" != *"dup restore"* ]]
}

@test "an unknown live-dump scope is refused" {
    run bash "$SCRIPT" live-dump "$DIR" prod /tmp/out.zip sideways
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown scope"* ]]
}

@test "restore-sql pulls a single dump out of duplicity" {
    run bash "$SCRIPT" restore-sql "$DIR" prod latest
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup sh -c"* ]]
    [[ "$(cat "$CALLS")" == *"dup restore --force"* ]]
    [[ "$(cat "$CALLS")" == *"--path-to-restore prod.sql"* ]]
    [[ "$(cat "$CALLS")" == *'"$DST"'* ]]
}

@test "restore-full restores the whole tree, unfiltered" {
    run bash "$SCRIPT" restore-full "$DIR" latest
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"dup restore --force"* ]]
    [[ "$(cat "$CALLS")" != *"--path-to-restore"* ]]
}

@test "'latest' omits the --time flag" {
    run bash "$SCRIPT" restore-sql "$DIR" prod latest
    [[ "$(cat "$CALLS")" != *"--time"* ]]
}

@test "an explicit timestamp becomes a --time flag" {
    run bash "$SCRIPT" restore-sql "$DIR" prod 2026-03-19T02:00:00
    [[ "$(cat "$CALLS")" == *"--time 2026-03-19T02:00:00"* ]]
}

@test "package-sql copies the dump out and zips it" {
    run bash "$SCRIPT" package-sql "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose cp backup:/tmp/bkdl/prod.sql $TMPDIR_ARG/dump.sql"* ]]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql"* ]]
    [ ! -d "$TMPDIR_ARG" ]
}

@test "package-sql cleans the container scratch dir" {
    run bash "$SCRIPT" package-sql "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [[ "$(cat "$CALLS")" == *"docker compose exec -T backup rm -rf /tmp/bkdl"* ]]
}

@test "package-full renames the dump and adds the filestore" {
    # Stand in for what `docker compose cp` would have dropped there.
    mkdir -p "$TMPDIR_ARG/odoo/filestore/prod"
    : > "$TMPDIR_ARG/prod.sql"
    run bash "$SCRIPT" package-full "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose cp backup:/tmp/bkdl/. $TMPDIR_ARG/"* ]]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql filestore"* ]]
    [ ! -d "$TMPDIR_ARG" ]
}

@test "package-full survives an instance with no filestore" {
    # The filestore copy is best-effort: an instance that has none must
    # still produce an archive with the dump in it.
    mkdir -p "$TMPDIR_ARG"
    : > "$TMPDIR_ARG/prod.sql"
    run bash "$SCRIPT" package-full "$DIR" prod "$TMPDIR_ARG" /tmp/out.zip
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"zip -r /tmp/out.zip dump.sql filestore"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" restore-sql "~/projects/inst" prod latest
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" restore-sql "$HOME/projects/gone" prod latest
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" prod
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}
