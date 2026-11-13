#!/usr/bin/env bats
# Tests for scripts/proxy_start.sh.
#
# ``docker`` is stubbed: every call lands in $CALLS, and ``up`` fails the
# first time when STUB_UP_FAIL is set, as a port already taken does. The
# proxy's state is ``running`` unless STUB_EXITED is set, which makes it
# ``exited`` until the previous files come back (a Traefik that stops on
# a static file it cannot parse). With STUB_LOOPING the container looks
# ``running`` at every check but its restart count grows with each
# ``restart`` and each check, as a Traefik in a restart loop does, until
# the previous files come back. ``compose version --short`` answers
# STUB_COMPOSE_VERSION, nothing when it is not set.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/proxy_start.sh"
    TMP="$(mktemp -d)"
    export IC_TRAEFIK_DIR="$TMP/traefik"
    export CALLS="$TMP/calls"
    export IC_PROXY_SETTLE=0
    mkdir -p "$TMP/bin" "$IC_TRAEFIK_DIR/dynamic"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$CALLS"
case "$*" in
    "compose version --short") echo "${STUB_COMPOSE_VERSION:-}" ;;
    *" up -d "*)
        if [ -n "${STUB_UP_FAIL:-}" ] && [ ! -e "$CALLS.failed" ]; then
            touch "$CALLS.failed"
            echo "Error: port is already allocated" >&2
            exit 1
        fi ;;
    *" ps -q proxy") echo "abc123" ;;
    "inspect "*RestartCount*)
        if [ -n "${STUB_LOOPING:-}" ] \
            && ! grep -q '^old' "$IC_TRAEFIK_DIR/traefik.yml"; then
            n="$(cat "$CALLS.count" 2>/dev/null || echo 0)"
            echo $((n + 1)) > "$CALLS.count"
            echo "$n"
        else
            echo 0
        fi ;;
    "inspect "*)
        if [ -n "${STUB_EXITED:-}" ] \
            && ! grep -q '^old' "$IC_TRAEFIK_DIR/traefik.yml"; then
            echo "exited"
        else
            echo "running"
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

@test "a proxy that starts and stops puts the old files back and fails" {
    bash "$SCRIPT" keep
    new_files
    STUB_EXITED=1 run bash "$SCRIPT" start
    [ "$status" -ne 0 ]
    for f in inverseproxy.yaml traefik.yml config.yml; do
        [ "$(cat "$IC_TRAEFIK_DIR/$f")" = "old $f" ]
    done
    [ "$(grep -c ' up -d --remove-orphans' "$CALLS")" -eq 2 ]
    [[ "$output" == *"started and stopped"* ]]
}

@test "a proxy that looks running but keeps restarting puts the old files back" {
    bash "$SCRIPT" keep
    new_files
    STUB_LOOPING=1 run bash "$SCRIPT" start
    [ "$status" -ne 0 ]
    for f in inverseproxy.yaml traefik.yml config.yml; do
        [ "$(cat "$IC_TRAEFIK_DIR/$f")" = "old $f" ]
    done
    [ "$(grep -c ' up -d --remove-orphans' "$CALLS")" -eq 2 ]
    [[ "$output" == *"restarting"* ]]
}

# A compose file with the proxy's fixed address on ``shared``.
mac_file() {
    printf '%s\n' \
        "services:" \
        "  proxy:" \
        "    networks:" \
        "      shared:" \
        "        mac_address: \"02:69:63:00:00:01\"" \
        "      private:" \
        > "$IC_TRAEFIK_DIR/inverseproxy.yaml"
}

@test "a Compose before 2.24 starts the proxy without its fixed address" {
    bash "$SCRIPT" keep
    mac_file
    STUB_COMPOSE_VERSION=v2.23.3 run bash "$SCRIPT" start
    [ "$status" -eq 0 ]
    ! grep -q "mac_address" "$IC_TRAEFIK_DIR/inverseproxy.yaml"
    grep -qx "      shared:" "$IC_TRAEFIK_DIR/inverseproxy.yaml"
    grep -qx "      private:" "$IC_TRAEFIK_DIR/inverseproxy.yaml"
    [[ "$output" == *"WARNING: Docker Compose 2.23.3 is older than 2.24"* ]]
    # What was put aside keeps the file as it ran.
    [ "$(cat "$IC_TRAEFIK_DIR/.ic-prev-inverseproxy.yaml")" = "old inverseproxy.yaml" ]
}

@test "a Compose from 2.24, or one that does not say, keeps the address" {
    for version in 2.24.0 5.7.0 ""; do
        mac_file
        STUB_COMPOSE_VERSION="$version" run bash "$SCRIPT" start
        [ "$status" -eq 0 ]
        grep -q 'mac_address: "02:69:63:00:00:01"' "$IC_TRAEFIK_DIR/inverseproxy.yaml"
        [[ "$output" != *"older than 2.24"* ]]
    done
}
