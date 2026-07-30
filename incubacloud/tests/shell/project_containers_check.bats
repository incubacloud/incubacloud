#!/usr/bin/env bats
# Tests for scripts/project_containers_check.sh — the guard that stops a
# project directory being deleted while containers still exist under it.
#
# ``docker`` is stubbed to return a canned list of compose working-dir
# labels, which is what the real check reads.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/project_containers_check.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    DIR="$HOME/acme"
    mkdir -p "$DIR"

    mkdir -p "$TMP/bin"
    export DOCKER_OUTPUT="$TMP/docker_output"
    : > "$DOCKER_OUTPUT"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
cat "$DOCKER_OUTPUT"
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "a clean project directory passes" {
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
    [[ "$output" == *"no containers found"* ]]
}

@test "a container under the directory blocks the delete" {
    echo "$DIR/inst" > "$DOCKER_OUTPUT"
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
    [[ "$output" == *"refusing to delete"* ]]
}

@test "a stopped container blocks it too" {
    # The check reads `docker ps -a`, so stopped containers count: a
    # stray container keeps the volume and the folder alive.
    echo "$DIR/old-inst" > "$DOCKER_OUTPUT"
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 1 ]
}

@test "containers of another project do not block it" {
    echo "$HOME/other/inst" > "$DOCKER_OUTPUT"
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
}

@test "a sibling directory with a shared prefix does not block it" {
    # ``acme-staging`` must not look like it lives under ``acme``.
    mkdir -p "$HOME/acme-staging"
    echo "$HOME/acme-staging/inst" > "$DOCKER_OUTPUT"
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 0 ]
}

@test "a missing directory is nothing to check" {
    run bash "$SCRIPT" "$HOME/gone"
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to check"* ]]
}

@test "a tilde path resolves against HOME" {
    run bash "$SCRIPT" "~/acme"
    [ "$status" -eq 0 ]
}

@test "a missing argument is refused" {
    run bash "$SCRIPT"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage:"* ]]
}
