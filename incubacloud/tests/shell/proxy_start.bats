#!/usr/bin/env bats
# Tests for scripts/proxy_start.sh.
#
# ``docker`` is stubbed: every call lands in $CALLS, and ``up`` fails the
# first time when STUB_UP_FAIL is set, as a port already taken does.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/proxy_start.sh"
    TMP="$(mktemp -d)"
    export IC_TRAEFIK_DIR="$TMP/traefik"
    export CALLS="$TMP/calls"
    mkdir -p "$TMP/bin" "$IC_TRAEFIK_DIR/dynamic"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$*" in
    *" up -d "*)
        if [ -n "${STUB_UP_FAIL:-}" ] && [ ! -e "$CALLS.failed" ]; then
            touch "$CALLS.failed"
            echo "Error: port is already allocated" >&2
            exit 1
        fi ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
    for f in inverseproxy.yaml traefik.yml config.yml; do
        echo "old $f" > "$IC_TRAEFIK_DIR/$f"
    done
}

teardown() {
    rm -rf "$TMP"
}

new_files() {
    for f in inverseproxy.yaml traefik.yml config.yml; do
        echo "new $f" > "$IC_TRAEFIK_DIR/$f"
    done
}

@test "keep puts the three running files aside" {
    run bash "$SCRIPT" keep
    [ "$status" -eq 0 ]
    for f in inverseproxy.yaml traefik.yml config.yml; do
        [ "$(cat "$IC_TRAEFIK_DIR/.ic-prev-$f")" = "old $f" ]
    done
}

@test "start brings the proxy up with the new files" {
    bash "$SCRIPT" keep
    new_files
    run bash "$SCRIPT" start
    [ "$status" -eq 0 ]
    grep -q -- "-p inverseproxy -f inverseproxy.yaml up -d --remove-orphans" "$CALLS"
    grep -q -- "-p inverseproxy -f inverseproxy.yaml restart proxy" "$CALLS"
    [ "$(cat "$IC_TRAEFIK_DIR/traefik.yml")" = "new traefik.yml" ]
}

@test "a refused start puts the old files back, starts them and fails" {
    bash "$SCRIPT" keep
    new_files
    STUB_UP_FAIL=1 run bash "$SCRIPT" start
    [ "$status" -ne 0 ]
    for f in inverseproxy.yaml traefik.yml config.yml; do
        [ "$(cat "$IC_TRAEFIK_DIR/$f")" = "old $f" ]
    done
    [ "$(cat "$IC_TRAEFIK_DIR/dynamic/config.yml")" = "old config.yml" ]
    [ "$(grep -c ' up -d --remove-orphans' "$CALLS")" -eq 2 ]
}

@test "a first setup has nothing to fall back to and fails" {
    new_files
    STUB_UP_FAIL=1 run bash "$SCRIPT" start
    [ "$status" -ne 0 ]
    [ "$(grep -c ' up -d ' "$CALLS")" -eq 1 ]
}
