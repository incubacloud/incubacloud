#!/usr/bin/env bats
# Tests for scripts/restore.sh — restore a doodba DB from an uploaded
# zip. ``docker`` and ``sudo`` are stubbed to record their argv.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/restore.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR"
    ZIP="$TMP/restore.zip"
    : > "$ZIP"

    mkdir -p "$TMP/bin"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    # docker: id -u prints a fake container UID; everything else records.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"id -u"*) echo "1234" ;;
esac
STUB
    # sudo: record and run nothing (chown/chmod would need real perms).
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
echo "sudo $*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/docker" "$TMP/bin/sudo"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "verify-file discovers the odoo UID and hands the zip over to it" {
    run bash "$SCRIPT" verify-file "$DIR" "$ZIP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *'docker compose run --rm --entrypoint= odoo id -u'* ]]
    [[ "$(cat "$CALLS")" == *"sudo chown 1234:1234 $ZIP"* ]]
    [[ "$(cat "$CALLS")" == *"sudo chmod 600 $ZIP"* ]]
}

@test "verify-file chowns before it chmods" {
    run bash "$SCRIPT" verify-file "$DIR" "$ZIP"
    chown_line="$(grep -n 'chown' "$CALLS" | head -1 | cut -d: -f1)"
    chmod_line="$(grep -n 'chmod' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$chown_line" -lt "$chmod_line" ]
}

@test "verify-file fails loudly when the upload is missing" {
    run bash "$SCRIPT" verify-file "$DIR" "$TMP/gone.zip"
    [ "$status" -eq 1 ]
    [[ "$output" == *"backup file not found"* ]]
}

@test "verify-file aborts if the container UID cannot be discovered" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
:
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" verify-file "$DIR" "$ZIP"
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not discover the odoo container UID"* ]]
}

@test "restore-db mounts the zip read-only and copies over the DB" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --rm -v $ZIP:/mnt/restore.zip:ro odoo click-odoo-restoredb --copy --force prod /mnt/restore.zip"* ]]
}

@test "ensure-connect reinstalls the connect module headless" {
    run bash "$SCRIPT" ensure-connect "$DIR" prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --rm odoo odoo -d prod -i incubacloud_connect --stop-after-init --no-http"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" ensure-connect "~/projects/inst" prod
    [ "$status" -eq 0 ]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" arg
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}
