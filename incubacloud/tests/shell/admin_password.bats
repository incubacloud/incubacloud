#!/usr/bin/env bats
# Tests for scripts/admin_password.sh.
#
# ``docker`` is stubbed: the call that runs the program records its
# arguments, takes the program on stdin and answers ``STUB_ANSWER``
# (nothing when empty) with ``STUB_STATUS``; the restart compose_op.sh
# makes exits ``STUB_RESTART_STATUS``; the compose's services and the
# project's containers are ``STUB_SERVICES`` (none when empty); every
# other call is recorded and succeeds. The program itself runs against databases of each series in
# tests/test_admin_password.py.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/admin_password.sh"
    TMP="$(mktemp -d)"
    DIR="$TMP/inst"
    mkdir -p "$DIR" "$TMP/bin"
    export DOCKER_CALLS="$TMP/calls" PROGRAM="$TMP/program"
    export STUB_ANSWER="ADMIN_PASSWORD changed" STUB_STATUS=0 STUB_RESTART_STATUS=0
    export STUB_SERVICES=""
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$DOCKER_CALLS"
case "$*" in
    *" odoo sh -c "*)
        cat > "$PROGRAM"
        echo "[entrypoint] noise"
        [ -z "$STUB_ANSWER" ] || echo "$STUB_ANSWER"
        exit "$STUB_STATUS" ;;
    *"compose restart odoo"*) exit "$STUB_RESTART_STATUS" ;;
    *"compose config --services"*)
        [ -z "$STUB_SERVICES" ] || echo "$STUB_SERVICES" ;;
    "ps -a --filter "*)
        [ -z "$STUB_SERVICES" ] || echo "$STUB_SERVICES|" ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "stopped: a one-off container of odoo, no restart" {
    run bash "$SCRIPT" "$DIR" prod stopped
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "ADMIN_PASSWORD changed" ]
    grep -q -- "^compose run --label traefik.enable=false --label sablier.enable=false --rm -T -e IC_DB=prod -e IC_SINCE=0 odoo sh -c " "$DOCKER_CALLS"
    ! grep -q "restart" "$DOCKER_CALLS" || false
    grep -q "ADMIN_PASSWORD changed" "$PROGRAM"
}

@test "running: the odoo container, and Odoo restarted after a change" {
    run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "ADMIN_PASSWORD changed" ]
    grep -q -- "^compose exec -T -e IC_DB=prod -e IC_SINCE=0 odoo sh -c " "$DOCKER_CALLS"
    grep -q -- "^compose restart odoo$" "$DOCKER_CALLS"
}

@test "running: a restart that fails fails the script, with no answer" {
    STUB_RESTART_STATUS=1 run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -ne 0 ]
    [[ "$output" == *"Odoo could not be restarted"* ]]
    # The program's own line is on its stdout, captured: what reaches the
    # caller is no answer line of the script.
    ! grep -qx "ADMIN_PASSWORD changed" <<<"$output" || false
}

@test "running: a restart on the previous build a failed rebuild marked counts" {
    # compose_op.sh exits 27: Odoo was restarted, on the containers the
    # rebuild left as they were.
    printf 'job=7\nkind=build\n' > "$DIR/.incubacloud-unapplied"
    STUB_SERVICES=odoo run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "ADMIN_PASSWORD changed" ]
    grep -q -- "^compose restart odoo$" "$DOCKER_CALLS"
}

@test "running: a restart the mark refuses fails the script, with no answer" {
    # compose_op.sh exits 26: the containers do not match the compose, and
    # nothing was restarted.
    printf 'job=7\nkind=build\n' > "$DIR/.incubacloud-unapplied"
    run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -ne 0 ]
    [[ "$output" == *"Odoo could not be restarted (exit 26)"* ]]
    ! grep -q "restart odoo" "$DOCKER_CALLS" || false
    ! grep -qx "ADMIN_PASSWORD changed" <<<"$output" || false
}

@test "the time of the last answer reaches the program, 0 by default" {
    run bash "$SCRIPT" "$DIR" prod stopped
    [ "$status" -eq 0 ]
    grep -q -- " -e IC_SINCE=0 odoo sh -c " "$DOCKER_CALLS"
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$DIR" prod running 1792369056
    [ "$status" -eq 0 ]
    grep -q -- "^compose exec -T -e IC_DB=prod -e IC_SINCE=1792369056 odoo sh -c " "$DOCKER_CALLS"
}

@test "a time that is not a number stops before docker" {
    run bash "$SCRIPT" "$DIR" prod running "1; rm -rf /"
    [ "$status" -ne 0 ]
    [ ! -e "$DOCKER_CALLS" ]
}

@test "running: a password of its own restarts nothing" {
    STUB_ANSWER="ADMIN_PASSWORD own" run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "ADMIN_PASSWORD own" ]
    ! grep -q "restart" "$DOCKER_CALLS" || false
}

@test "absent is an answer too" {
    STUB_ANSWER="ADMIN_PASSWORD absent" run bash "$SCRIPT" "$DIR" prod stopped
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "ADMIN_PASSWORD absent" ]
}

@test "a program that fails fails the script" {
    STUB_ANSWER="" STUB_STATUS=3 run bash "$SCRIPT" "$DIR" prod stopped
    [ "$status" -ne 0 ]
    [[ "$output" == *"could not check the administrator's password"* ]]
}

@test "no answer fails the script" {
    STUB_ANSWER="" run bash "$SCRIPT" "$DIR" prod running
    [ "$status" -ne 0 ]
    [[ "$output" == *"gave no answer"* ]]
    ! grep -q "restart" "$DOCKER_CALLS" || false
}

@test "an unknown mode stops before docker" {
    run bash "$SCRIPT" "$DIR" prod sideways
    [ "$status" -ne 0 ]
    [ ! -e "$DOCKER_CALLS" ]
}

@test "a missing directory stops before docker" {
    run bash "$SCRIPT" "$TMP/nowhere" prod stopped
    [ "$status" -ne 0 ]
    [ ! -e "$DOCKER_CALLS" ]
}

@test "the program prints neither the password nor the hash" {
    run bash "$SCRIPT" "$DIR" prod stopped
    [ "$status" -eq 0 ]
    # The only lines it prints are its answers and the absent user's.
    [ "$(grep -c 'print(' "$PROGRAM")" -eq 3 ]
    ! grep -n 'print(.*\(secret\|hashed\|update\)' "$PROGRAM" || false
    ! grep -n 'write(.*\(secret\|hashed\|update\)' "$PROGRAM" || false
}
