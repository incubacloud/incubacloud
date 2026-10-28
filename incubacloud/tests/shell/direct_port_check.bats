#!/usr/bin/env bats
# Tests for scripts/direct_port_check.sh.
#
# ``docker`` and ``ss`` are stubbed. With ``userland-proxy: false``
# (Cloud1) a port Docker publishes has no socket at all, so the check has
# to read Docker's own list as well as the host's listeners.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/direct_port_check.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ "$1" = ps ] || exit 2
printf '%s\n' "${STUB_PS:-}"
STUB
    cat > "$TMP/bin/ss" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "${STUB_SS:-}" | sed '/^$/d'
STUB
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "$1" = -n ] && shift
exec "$@"
STUB
    chmod +x "$TMP/bin/docker" "$TMP/bin/ss" "$TMP/bin/sudo"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

@test "a free port is free" {
    STUB_PS='incubacloud-central-vmauth-1 172.17.0.1:8428->8428/tcp' run bash "$SCRIPT" 9443
    [ "$status" -eq 0 ]
    [ "$output" = "IC_DIRECT_PORT_FREE" ]
}

@test "a port Docker publishes without a socket is busy" {
    STUB_PS='incubacloud-central-grafana-1 172.17.0.1:3000->3000/tcp' run bash "$SCRIPT" 3000
    [ "$status" -eq 1 ]
    [[ "$output" == "IC_DIRECT_PORT_BUSY container incubacloud-central-grafana-1"* ]]
}

@test "a port published on any address is busy" {
    for ports in '0.0.0.0:9443->9443/tcp' '127.0.0.1:9443->80/tcp' '[::]:9443->9443/tcp'; do
        STUB_PS="other-1 $ports" run bash "$SCRIPT" 9443
        [ "$status" -eq 1 ]
    done
}

@test "a process listening on it is busy" {
    STUB_SS='LISTEN 0 4096 0.0.0.0:9443 0.0.0.0:* users:(("nginx",pid=7,fd=6))' run bash "$SCRIPT" 9443
    [ "$status" -eq 1 ]
    [[ "$output" == *'process users:(("nginx"'* ]]
}

@test "both at once are reported" {
    STUB_PS='other-1 0.0.0.0:9443->9443/tcp' \
    STUB_SS='LISTEN 0 4096 0.0.0.0:9443 0.0.0.0:* users:(("nginx",pid=7,fd=6))' \
        run bash "$SCRIPT" 9443
    [ "$status" -eq 1 ]
    [[ "$output" == *"container other-1"*"process"* ]]
}

@test "the proxy itself publishing it is a second Full Setup, not a conflict" {
    STUB_PS='inverseproxy-proxy-1 0.0.0.0:443->443/tcp, 0.0.0.0:9443->9443/tcp' \
    STUB_SS='LISTEN 0 4096 0.0.0.0:9443 0.0.0.0:* users:(("docker-proxy",pid=9,fd=4))' \
        run bash "$SCRIPT" 9443
    [ "$status" -eq 0 ]
}

@test "a port that is not one is refused" {
    run bash "$SCRIPT" 80a
    [ "$status" -ne 0 ]
}
