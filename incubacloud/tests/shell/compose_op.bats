#!/usr/bin/env bats
# Tests for scripts/compose_op.sh.
#
# ``docker`` is stubbed on PATH so the script's own logic is what gets
# exercised: argument handling, tilde expansion, the missing-directory
# rules and the exact compose command each operation builds. The stub
# answers the two read-only queries the script makes (the stack's
# services, the running ones) from ``STUB_SERVICES`` / ``STUB_RUNNING``
# without recording them, so ``DOCKER_CALLS`` holds what changes state.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/compose_op.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    # Stub docker: record the argv it was called with, succeed.
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "compose config --services") printf '%b' "${STUB_SERVICES:-}"; exit 0 ;;
    "compose ps --status running --services") printf '%b' "${STUB_RUNNING:-}"; exit 0 ;;
esac
echo "docker $*" >> "$DOCKER_CALLS"
STUB
    chmod +x "$TMP/bin/docker"
    export DOCKER_CALLS="$TMP/calls"
    : > "$DOCKER_CALLS"
    export PATH="$TMP/bin:$PATH"
}

# A staging with an allowlist: the egress sidecar is in the stack and
# odoo runs.
with_sidecar() {
    export STUB_SERVICES="odoo\ndb\nsmtp\nproxy_general\nodoo_net_setup\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\nproxy_general\nodoo_net_setup\n"
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

@test "starting odoo restarts the egress sidecar after it" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo
docker compose restart odoo_net_setup" ]
}

@test "restarting the stack restarts the egress sidecar again, last" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$(cat "$DOCKER_CALLS")" = "docker compose restart
docker compose restart odoo_net_setup" ]
}

@test "up restarts the egress sidecar once odoo runs" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d
docker compose restart odoo_net_setup" ]
}

@test "the sidecar is left alone when odoo is not running" {
    with_sidecar
    export STUB_RUNNING="db\n"
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
}

@test "an operation that does not start odoo leaves the sidecar alone" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" start db
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start db" ]
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$HOME/project/inst" stop odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop odoo" ]
}

@test "restarting the sidecar itself does not restart it twice" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" restart odoo_net_setup
    [ "$(cat "$DOCKER_CALLS")" = "docker compose restart odoo_net_setup" ]
}
