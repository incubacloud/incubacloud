#!/usr/bin/env bats
# Tests for scripts/compose_op.sh.
#
# ``docker`` is stubbed on PATH so the script's own logic is what gets
# exercised: argument handling, tilde expansion, the missing-directory
# rules and the exact compose command each operation builds.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/compose_op.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    # Stub docker: record the argv it was called with, succeed.
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_CALLS"
STUB
    chmod +x "$TMP/bin/docker"
    export DOCKER_CALLS="$TMP/calls"
    : > "$DOCKER_CALLS"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "up starts the stack detached" {
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "stop without services stops the whole stack" {
    run bash "$SCRIPT" "$HOME/project/inst" stop
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop" ]
}

@test "stop with services stops only those" {
    run bash "$SCRIPT" "$HOME/project/inst" stop odoo backup
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop odoo backup" ]
}

@test "start starts existing containers" {
    run bash "$SCRIPT" "$HOME/project/inst" start
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start" ]
}

@test "start can target a single service" {
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
}

@test "restart restarts the stack" {
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$(cat "$DOCKER_CALLS")" = "docker compose restart" ]
}

@test "down removes images, volumes and orphans" {
    run bash "$SCRIPT" "$HOME/project/inst" down
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose down --rmi all -v --remove-orphans" ]
}

@test "a tilde path resolves against HOME" {
    run bash "$SCRIPT" "~/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "down on a missing directory succeeds without calling docker" {
    run bash "$SCRIPT" "$HOME/project/gone" down
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to shut down"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "up on a missing directory fails loudly" {
    run bash "$SCRIPT" "$HOME/project/gone" up
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" "$HOME/project/inst" explode
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "missing arguments are refused with a usage line" {
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage: compose_op.sh"* ]]
}

@test "a failing docker call fails the script" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
exit 3
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 3 ]
}
