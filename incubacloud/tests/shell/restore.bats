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
    *"name = 'incubacloud_connect'"*) [ -n "${CONNECT_STATE:-}" ] && echo "$CONNECT_STATE" ;;
    *"to_regclass('public.ir_config_parameter')"*)
        [ -z "${QUERY_FAILS:-}" ] || { echo 'psql: error: connection refused' >&2; exit 2; }
        echo "${HAS_PARAMS:-t}" ;;
    *"SELECT 1 FROM ir_config_parameter WHERE key = 'database.is_neutralized'"*)
        [ -z "${FLAG_FAILS:-}" ] || { echo 'ERROR: canceling statement' >&2; exit 1; }
        if [ -n "${NEUTRALIZED:-}" ]; then echo "$NEUTRALIZED"; fi ;;
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
    [[ "$(cat "$CALLS")" == *'docker compose run --label traefik.enable=false --label sablier.enable=false --rm --entrypoint= odoo id -u'* ]]
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

@test "verify-file never hands a link over to the odoo user" {
    # sudo chown follows a link: an uploaded "archive" pointing at a file
    # of the host's would give that file to the container's user.
    printf 'root:x:0:0' > "$TMP/hostfile"
    ln -s "$TMP/hostfile" "$TMP/link.zip"
    run bash "$SCRIPT" verify-file "$DIR" "$TMP/link.zip"
    [ "$status" -ne 0 ]
    [[ "$(cat "$CALLS")" != *"sudo chown"* ]]
    [[ "$(cat "$CALLS")" != *"sudo chmod"* ]]
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
    [[ "$(cat "$CALLS")" == *"docker compose run --label traefik.enable=false --label sablier.enable=false --rm -v $ZIP:/mnt/restore.zip:ro odoo click-odoo-restoredb --copy --force prod /mnt/restore.zip"* ]]
}

@test "restore-db does not neutralize unless asked" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" != *"--neutralize"* ]]
}

@test "restore-db neutralizes when the 4th argument is 1" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 1
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"click-odoo-restoredb --neutralize --copy --force prod /mnt/restore.zip"* ]]
}

@test "restore-db loads with the series' tool the panel names" {
    IC_DB_TOOL=odoo-db run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 1
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --label traefik.enable=false --label sablier.enable=false --rm -v $ZIP:/mnt/restore.zip:ro odoo odoo db load --neutralize --force prod /mnt/restore.zip"* ]]
    [[ "$(cat "$CALLS")" != *"click-odoo"* ]]
}

@test "restore-db loads a move as moved, keeping the database's secret" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 0 move
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"odoo click-odoo-restoredb --move --force prod /mnt/restore.zip"* ]]
    [[ "$(cat "$CALLS")" != *"--copy"* ]]
}

@test "restore-db treats an explicit 0 as no neutralization" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 0
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" != *"--neutralize"* ]]
}

@test "restore-db drops the list of paused crons a neutralized copy brings" {
    # A dump taken while production's update had its crons paused carries
    # cron_guard.sh's list; the update of the copy would switch them on.
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 1
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"odoo psql -v ON_ERROR_STOP=1 -d prod -c DROP TABLE IF EXISTS incubacloud_cron_pause"* ]]
    load="$(grep -n 'restoredb' "$CALLS" | head -1 | cut -d: -f1)"
    drop="$(grep -n 'incubacloud_cron_pause' "$CALLS" | head -1 | cut -d: -f1)"
    [ "$load" -lt "$drop" ]
}

@test "restore-db keeps the list of a copy that is not neutralized" {
    run bash "$SCRIPT" restore-db "$DIR" prod "$ZIP" 0
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" != *"incubacloud_cron_pause"* ]]
}

@test "neutralize runs Odoo's own neutralization on the database" {
    run bash "$SCRIPT" neutralize "$DIR" prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"docker compose run --label traefik.enable=false --label sablier.enable=false --rm odoo odoo neutralize -d prod"* ]]
}

@test "neutralize runs the panel's SQL on a series without Odoo's" {
    IC_NEUTRALIZE=sql run bash "$SCRIPT" neutralize "$DIR" prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"odoo psql -v ON_ERROR_STOP=1 -d prod -c"*"UPDATE ir_cron SET active = false"* ]]
    [[ "$(cat "$CALLS")" != *"odoo neutralize"* ]]
}

@test "neutralize refuses to run without the database" {
    run bash "$SCRIPT" neutralize "$DIR"
    [ "$status" -ne 0 ]
}

@test "set-base-url upserts both parameters and drops the freeze" {
    run bash "$SCRIPT" set-base-url "$DIR" odoo prod \
        "https://staging.example.com" "http://localhost:8069"
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"docker compose exec -T db psql -U odoo -d prod -c"* ]]
    [[ "$calls" == *"('web.base.url','https://staging.example.com'),('report.url','http://localhost:8069')"* ]]
    [[ "$calls" == *"DELETE FROM ir_config_parameter WHERE key='web.base.url.freeze'"* ]]
}

@test "set-base-url is guarded against a database with no parameters table" {
    run bash "$SCRIPT" set-base-url "$DIR" odoo prod "https://x.example.com" \
        "http://localhost:8069"
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"table_name='ir_config_parameter'"* ]]
}

@test "set-base-url refuses to run with missing arguments" {
    run bash "$SCRIPT" set-base-url "$DIR" odoo prod
    [ "$status" -eq 1 ]
}

@test "use-mail-catcher deletes only the neutralization placeholder" {
    run bash "$SCRIPT" use-mail-catcher "$DIR" odoo prod
    [ "$status" -eq 0 ]
    calls="$(cat "$CALLS")"
    [[ "$calls" == *"docker compose exec -T db psql -U odoo -d prod -c"* ]]
    [[ "$calls" == *"DELETE FROM ir_mail_server WHERE name='neutralization - disable emails' AND smtp_host='invalid'"* ]]
}

@test "use-mail-catcher is guarded against a database with no mail servers table" {
    run bash "$SCRIPT" use-mail-catcher "$DIR" odoo prod
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"table_name='ir_mail_server'"* ]]
}

@test "use-mail-catcher refuses to run with missing arguments" {
    run bash "$SCRIPT" use-mail-catcher "$DIR" odoo
    [ "$status" -eq 1 ]
}

@test "renew-neutralization neutralizes a neutralized database again, then routes its mail to the catcher" {
    NEUTRALIZED=1 run bash "$SCRIPT" renew-neutralization "$DIR" odoo prod
    [ "$status" -eq 0 ]
    neutralize="$(grep -n "odoo odoo neutralize -d prod" "$CALLS" | cut -d: -f1)"
    catcher="$(grep -n "DELETE FROM ir_mail_server WHERE name='neutralization - disable emails'" "$CALLS" | cut -d: -f1)"
    [ -n "$neutralize" ] && [ -n "$catcher" ] && [ "$neutralize" -lt "$catcher" ]
}

@test "renew-neutralization uses the panel's SQL on a series without Odoo's" {
    NEUTRALIZED=1 IC_NEUTRALIZE=sql run bash "$SCRIPT" renew-neutralization "$DIR" odoo prod
    [ "$status" -eq 0 ]
    grep -q "odoo psql -v ON_ERROR_STOP=1 -d prod -c" "$CALLS"
    ! grep -q "odoo neutralize" "$CALLS"
}

@test "renew-neutralization leaves a database that is not neutralized as it is" {
    run bash "$SCRIPT" renew-neutralization "$DIR" odoo prod
    [ "$status" -eq 0 ]
    [[ "$output" == *"not neutralized"* ]]
    grep -q "database.is_neutralized" "$CALLS"
    ! grep -q "neutralize -d" "$CALLS"
    ! grep -q "DELETE FROM ir_mail_server" "$CALLS"
}

@test "renew-neutralization dies when it cannot read whether the database is neutralized" {
    # A rebuild of a neutralized staging would otherwise start it with
    # its scheduled actions and mail servers on again.
    for fails in QUERY_FAILS FLAG_FAILS; do
        : > "$CALLS"
        run env NEUTRALIZED=1 "$fails=1" bash "$SCRIPT" renew-neutralization "$DIR" odoo prod
        [ "$status" -ne 0 ]
        [[ "$output" == *"could not read whether prod is neutralized"* ]]
        ! grep -q "neutralize -d" "$CALLS"
    done
}

@test "renew-neutralization leaves a database without Odoo's parameters as it is" {
    HAS_PARAMS=f NEUTRALIZED=1 run bash "$SCRIPT" renew-neutralization "$DIR" odoo prod
    [ "$status" -eq 0 ]
    [[ "$output" == *"not neutralized"* ]]
    ! grep -q "neutralize -d" "$CALLS"
}

@test "renew-neutralization refuses to run with missing arguments" {
    run bash "$SCRIPT" renew-neutralization "$DIR" odoo
    [ "$status" -eq 1 ]
}

@test "ensure-connect installs the connect module headless when it is missing" {
    run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --no-http
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"psql -U odoo -d prod"* ]]
    [[ "$(cat "$CALLS")" == *"docker compose run --label traefik.enable=false --label sablier.enable=false --rm odoo odoo -d prod -i incubacloud_connect --stop-after-init --no-http"* ]]
}

@test "ensure-connect goes headless with the option the panel hands it" {
    # 8.0 to 10.0 have no --no-http; the series' card says --no-xmlrpc.
    run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --no-xmlrpc
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"odoo -d prod -i incubacloud_connect --stop-after-init --no-xmlrpc"* ]]
    [[ "$(cat "$CALLS")" != *"--no-http"* ]]
}

@test "ensure-connect asks Odoo nothing to choose the option" {
    run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --no-http
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" != *"--help"* ]]
}

@test "ensure-connect refuses an option that is not a headless one" {
    run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --dev=all
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown headless option"* ]]
    [[ "$(cat "$CALLS")" != *"docker"* ]]
}

@test "ensure-connect refuses to run without the headless option" {
    run bash "$SCRIPT" ensure-connect "$DIR" odoo prod
    [ "$status" -eq 1 ]
    [[ "$(cat "$CALLS")" != *"docker"* ]]
}

@test "ensure-connect boots no Odoo for a module already installed" {
    CONNECT_STATE=installed run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --no-http
    [ "$status" -eq 0 ]
    [[ "$output" == *"already installed"* ]]
    [[ "$(cat "$CALLS")" != *"docker compose run"* ]]
}

@test "ensure-connect installs when the module is only half there" {
    CONNECT_STATE="to install" run bash "$SCRIPT" ensure-connect "$DIR" odoo prod --no-http
    [ "$status" -eq 0 ]
    [[ "$(cat "$CALLS")" == *"-i incubacloud_connect"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    run bash "$SCRIPT" ensure-connect "~/projects/inst" odoo prod --no-http
    [ "$status" -eq 0 ]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" explode "$DIR" arg
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
}

@test "verify-file and restore-db never print where the archive is" {
    # A handoff restored in place is named with the panel's secret.
    mkdir -p "$TMP/handoff-secretname"
    : > "$TMP/handoff-secretname/archive.zip"
    run bash "$SCRIPT" verify-file "$DIR" "$TMP/handoff-secretname/archive.zip"
    [ "$status" -eq 0 ]
    [[ "$output" != *secretname* ]]
    run bash "$SCRIPT" restore-db "$DIR" prod "$TMP/handoff-secretname/archive.zip" 1
    [ "$status" -eq 0 ]
    [[ "$output" != *secretname* ]]
    run bash "$SCRIPT" verify-file "$DIR" "$TMP/handoff-secretname/gone.zip"
    [ "$status" -ne 0 ]
    [[ "$output" != *secretname* ]]
}
