#!/usr/bin/env bats
# Tests for scripts/move_health_wait.sh — the pre-cutover health gate.
# ``docker`` and ``sleep`` are stubbed so the poll loop runs instantly.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/move_health_wait.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR" "$TMP/bin"

    # A no-op sleep keeps the ~90s loop instant under test.
    cat > "$TMP/bin/sleep" <<'STUB'
#!/usr/bin/env bash
:
STUB
    chmod +x "$TMP/bin/sleep"
    export PATH="$TMP/bin:$PATH"
    export HEALTH_STATE="$TMP/health"
}

teardown() {
    rm -rf "$TMP"
}

# docker stub whose curl succeeds or fails based on HEALTH_STATE.
_stub_docker() {
    cat > "$TMP/bin/docker" <<STUB
#!/usr/bin/env bash
# Only the curl health probe matters here.
if [ "\$1" = "compose" ]; then
    [ -f "$1" ]
    exit \$?
fi
exit 0
STUB
    chmod +x "$TMP/bin/docker"
}

@test "reports health:ok and exits 0 as soon as the endpoint answers" {
    _stub_docker "$HEALTH_STATE"
    : > "$HEALTH_STATE"   # healthy from the first probe
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"health:ok"* ]]
}

@test "reports health:timeout and exits 1 when it never comes up" {
    _stub_docker "$HEALTH_STATE"   # HEALTH_STATE never created → always fails
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"health:timeout"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    _stub_docker "$HEALTH_STATE"
    : > "$HEALTH_STATE"
    run bash "$SCRIPT" "~/projects/inst"
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    _stub_docker "$HEALTH_STATE"
    run bash "$SCRIPT" "$HOME/projects/gone"
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "a missing argument is refused" {
    run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}
