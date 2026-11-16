#!/usr/bin/env bats
# Tests for scripts/move_health_wait.sh — the pre-cutover health gate.
# ``docker`` and ``sleep`` are stubbed so the poll loop runs instantly;
# ``MOVE_HEALTH_DEADLINE_S`` keeps a wait that never ends short.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/move_health_wait.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR" "$TMP/bin"

    # A no-op sleep keeps the loop instant under test.
    cat > "$TMP/bin/sleep" <<'STUB'
#!/usr/bin/env bash
:
STUB
    chmod +x "$TMP/bin/sleep"
    export PATH="$TMP/bin:$PATH"
    export HEALTH_STATE="$TMP/health"
    export CALLS="$TMP/calls"
    : > "$CALLS"
}

teardown() {
    rm -rf "$TMP"
}

# docker stub: ``compose ps -q odoo`` names the container in $TMP/id (or
# none), and ``exec <id> curl`` succeeds when HEALTH_STATE exists, answers
# curl's 7 (refused) while the id is the current one, and docker's 1 (no
# such container) for any other id. Every call is logged to $CALLS.
_stub_docker() {
    cat > "$TMP/bin/docker" <<STUB
#!/usr/bin/env bash
echo "\$*" >> "$CALLS"
if [ "\$1" = "compose" ]; then
    [ -f "$TMP/id" ] && cat "$TMP/id"
    exit 0
fi
if [ "\$1" = "exec" ]; then
    [ "\$2" = "\$(cat "$TMP/id" 2>/dev/null)" ] || exit 1
    [ -f "$HEALTH_STATE" ] && exit 0
    exit 7
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
}

@test "reports health:ok and exits 0 as soon as the endpoint answers" {
    _stub_docker
    echo c1 > "$TMP/id"
    : > "$HEALTH_STATE"   # healthy from the first probe
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"health:ok"* ]]
    grep -q '^exec c1 curl -sf --max-time 2 ' "$CALLS"
    [ "$(grep -c '^compose ps -q odoo$' "$CALLS")" -eq 1 ]
}

@test "reports health:timeout and exits 1 when it never comes up" {
    _stub_docker
    echo c1 > "$TMP/id"   # HEALTH_STATE never created → always refused
    MOVE_HEALTH_DEADLINE_S=1 run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"health:timeout"* ]]
}

@test "the container is looked up once while curl is what fails" {
    _stub_docker
    echo c1 > "$TMP/id"
    MOVE_HEALTH_DEADLINE_S=1 run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
    [ "$(grep -c '^exec c1 ' "$CALLS")" -gt 1 ]
    [ "$(grep -c '^compose ps' "$CALLS")" -eq 1 ]
}

@test "a container recreated during the wait is found again" {
    _stub_docker
    echo c1 > "$TMP/id"
    # The first probe recreates the container: c1 is gone, c2 is healthy.
    real="$TMP/bin/docker"
    mv "$real" "$TMP/bin/docker-real"
    cat > "$real" <<STUB
#!/usr/bin/env bash
if [ "\$1" = "exec" ] && [ ! -f "$TMP/recreated" ]; then
    : > "$TMP/recreated"
    echo c2 > "$TMP/id"
    : > "$HEALTH_STATE"
fi
exec "$TMP/bin/docker-real" "\$@"
STUB
    chmod +x "$real"
    MOVE_HEALTH_DEADLINE_S=5 run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"health:ok"* ]]
    grep -q '^exec c1 ' "$CALLS"
    grep -q '^exec c2 ' "$CALLS"
}

@test "no container yet: it is looked up until there is one" {
    _stub_docker
    # No $TMP/id: compose names nothing, nothing is probed.
    MOVE_HEALTH_DEADLINE_S=1 run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"health:timeout"* ]]
    [ "$(grep -c '^exec ' "$CALLS")" -eq 0 ]
    [ "$(grep -c '^compose ps' "$CALLS")" -gt 1 ]
}

@test "a tilde instance directory resolves against HOME" {
    _stub_docker
    echo c1 > "$TMP/id"
    : > "$HEALTH_STATE"
    run bash "$SCRIPT" "~/projects/inst"
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    _stub_docker
    run bash "$SCRIPT" "$HOME/projects/gone"
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "a missing argument is refused" {
    run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}
