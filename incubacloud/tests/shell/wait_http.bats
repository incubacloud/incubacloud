#!/usr/bin/env bats
# Tests for scripts/wait_http.sh — poll an in-container HTTP endpoint.
# ``docker`` (whose curl succeeds/fails) and ``sleep`` are stubbed.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/wait_http.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR" "$TMP/bin"
    export READY_FLAG="$TMP/ready"

    cat > "$TMP/bin/sleep" <<'STUB'
#!/usr/bin/env bash
:
STUB
    # docker's inner curl succeeds only once READY_FLAG exists.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ -f "$READY_FLAG" ]
exit $?
STUB
    chmod +x "$TMP/bin/sleep" "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "succeeds as soon as the endpoint answers" {
    : > "$READY_FLAG"
    run bash "$SCRIPT" "$DIR" /web/login 60 1
    [ "$status" -eq 0 ]
}

@test "fails when the endpoint never answers" {
    run bash "$SCRIPT" "$DIR" /web/login 3 1
    [ "$status" -eq 1 ]
    [[ "$output" == *"did not answer"* ]]
}

@test "a tilde instance directory resolves against HOME" {
    : > "$READY_FLAG"
    run bash "$SCRIPT" "~/projects/inst" /web/login 60 1
    [ "$status" -eq 0 ]
}

@test "a missing instance directory fails loudly" {
    run bash "$SCRIPT" "$HOME/projects/gone" /web/login 60 1
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
}

@test "too few arguments are refused" {
    run bash "$SCRIPT" "$DIR" /web/login
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}
