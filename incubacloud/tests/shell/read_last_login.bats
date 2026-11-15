#!/usr/bin/env bats
# Tests for scripts/read_last_login.sh.
#
# The database password comes in a file the job staged, never as an
# argument: an argument is in the host's process list and in whatever
# logs the SSH session. ``docker`` is stubbed to record its arguments and
# the password its environment hands the container.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/read_last_login.sh"
    TMP="$(mktemp -d)"
    DIR="$TMP/inst"
    mkdir -p "$DIR" "$TMP/bin"
    export DOCKER_CALLS="$TMP/calls" DOCKER_ENV="$TMP/env"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
# docker compose exec -T -e IC_DB=<db> -e IC_USER=<user> -e IC_PASSWORD odoo ...
printf '%s\n' "$*" >> "$DOCKER_CALLS"
printf '%s' "${IC_PASSWORD-unset}" > "$DOCKER_ENV"
cat > /dev/null
echo "LAST_LOGIN none"
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
    PASSWORD_FILE="$TMP/db_password"
    printf '%s' 'pw with $(spaces) and "quotes"' > "$PASSWORD_FILE"
}

teardown() {
    rm -rf "$TMP"
}

@test "the password reaches the container's environment, never an argument" {
    run bash "$SCRIPT" "$DIR" prod odoo "$PASSWORD_FILE"
    [ "$status" -eq 0 ]
    [ "$output" = "LAST_LOGIN none" ]
    [ "$(cat "$DOCKER_ENV")" = 'pw with $(spaces) and "quotes"' ]
    grep -q -- " -e IC_PASSWORD odoo " "$DOCKER_CALLS"
    ! grep -q "quotes" "$DOCKER_CALLS"
}

@test "a missing password file stops before docker" {
    run bash "$SCRIPT" "$DIR" prod odoo "$TMP/nowhere"
    [ "$status" -ne 0 ]
    [[ "$output" == *"password file not found"* ]]
    [ ! -e "$DOCKER_CALLS" ]
}
