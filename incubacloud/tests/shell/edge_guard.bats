#!/usr/bin/env bats
# Tests for scripts/edge_guard.sh.
#
# ``docker``, ``curl``, ``sudo`` and ``sleep`` are stubbed; the proxy's
# directory is a temporary one (IC_TRAEFIK_DIR). The file is replaced only
# when it changed and by a rename, the proxy has to mount the directory
# (exit 3), and the probe says whether the proxy refuses the page (exit 0)
# could not be verified because the handshake was refused (exit 4), or
# answers with another HTTP status (exit 5). The staged copy never survives.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/edge_guard.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/bin" "$TMP/traefik/dynamic"
    export IC_TRAEFIK_DIR="$TMP/traefik"
    export IC_EDGE_GUARD_PROBE_WAIT=0
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ "$1" = inspect ] || exit 2
[ -n "${STUB_NO_PROXY:-}" ] && exit 1
printf '%s\n' "${STUB_MOUNTS-/etc/traefik/traefik.yml /etc/traefik/dynamic /var/log/traefik }"
STUB
    cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$STUB_CURL_LOG"
printf '%s' "${STUB_CODE:-403}"
[ "${STUB_CODE:-403}" = 000 ] && exit 35
exit 0
STUB
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$STUB_SUDO_LOG"
exec "$@"
STUB
    chmod +x "$TMP/bin/docker" "$TMP/bin/curl" "$TMP/bin/sudo"
    export PATH="$TMP/bin:$PATH"
    export STUB_CURL_LOG="$TMP/curl.log" STUB_SUDO_LOG="$TMP/sudo.log"
    : > "$STUB_CURL_LOG"
    : > "$STUB_SUDO_LOG"
    STAGED="$TMP/job/ic_guard.yml"
    mkdir -p "$TMP/job"
    printf 'http: {guard: 1}\n' > "$STAGED"
}

teardown() {
    chmod -R u+w "$TMP" 2>/dev/null || true
    rm -rf "$TMP"
}

@test "a missing guard is written, the proxy refuses the page: 0" {
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ic_guard.yml updated"* ]]
    [[ "$output" == *"IC_EDGE_GUARD_REFUSES"* ]]
    [ "$(cat "$TMP/traefik/dynamic/ic_guard.yml")" = "http: {guard: 1}" ]
    [ ! -e "$STAGED" ]
    [ ! -e "$TMP/traefik/.ic_guard.yml.new" ]
    grep -q "Host: ic-guard.invalid https://127.0.0.1/website/info" "$STUB_CURL_LOG"
}

@test "the same guard is left as it is" {
    cp "$STAGED" "$TMP/traefik/dynamic/ic_guard.yml"
    touch -d '2020-01-01' "$TMP/traefik/dynamic/ic_guard.yml"
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ic_guard.yml unchanged"* ]]
    [ "$(stat -c %Y "$TMP/traefik/dynamic/ic_guard.yml")" = "$(date -d '2020-01-01' +%s)" ]
    [ ! -e "$STAGED" ]
}

@test "a different guard is replaced by a rename, not written in place" {
    printf 'old\n' > "$TMP/traefik/dynamic/ic_guard.yml"
    before="$(stat -c %i "$TMP/traefik/dynamic/ic_guard.yml")"
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 0 ]
    [[ "$output" == *"ic_guard.yml updated"* ]]
    [ "$(cat "$TMP/traefik/dynamic/ic_guard.yml")" = "http: {guard: 1}" ]
    [ "$(stat -c %i "$TMP/traefik/dynamic/ic_guard.yml")" != "$before" ]
}

@test "a directory the account cannot write takes sudo" {
    chmod a-w "$TMP/traefik/dynamic" "$TMP/traefik"
    # The stub's sudo runs as the same account: give it back the rights
    # the real one has.
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$STUB_SUDO_LOG"
chmod u+w "$IC_TRAEFIK_DIR" "$IC_TRAEFIK_DIR/dynamic"
exec "$@"
STUB
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 0 ]
    grep -q "^sudo mv -f" "$STUB_SUDO_LOG"
    [ "$(cat "$TMP/traefik/dynamic/ic_guard.yml")" = "http: {guard: 1}" ]
}

@test "a proxy that does not mount the directory: written, exit 3" {
    STUB_MOUNTS='/etc/traefik/traefik.yml /etc/traefik/config.yml ' run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 3 ]
    [[ "$output" == *"IC_EDGE_GUARD_NOT_MOUNTED"* ]]
    [ -f "$TMP/traefik/dynamic/ic_guard.yml" ]
    [ ! -s "$STUB_CURL_LOG" ]
    [ ! -e "$STAGED" ]
}

@test "no proxy at all is not mounted either" {
    STUB_NO_PROXY=1 run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 3 ]
}

@test "Full Setup writes it before the proxy runs: no mount check, no probe" {
    STUB_NO_PROXY=1 run bash "$SCRIPT" install "$STAGED" --no-mount-check --no-probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"ic_guard.yml updated"* ]]
    [[ "$output" != *"IC_EDGE_GUARD"* ]]
    [ ! -s "$STUB_CURL_LOG" ]
}

@test "a real answer other than 403, five times, is not applied: exit 5" {
    STUB_CODE=404 run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 5 ]
    [[ "$output" == *"IC_EDGE_GUARD_NOT_APPLIED 404"* ]]
    [ "$(wc -l < "$STUB_CURL_LOG")" -eq 5 ]
    [ -f "$TMP/traefik/dynamic/ic_guard.yml" ]
}

@test "a refused handshake is not verified either" {
    STUB_CODE=000 run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 4 ]
    [[ "$output" == *"IC_EDGE_GUARD_UNVERIFIED 000"* ]]
}

@test "the staged copy goes even when the write fails" {
    chmod a-w "$TMP/traefik/dynamic" "$TMP/traefik"
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 1 ]
    [[ "$output" == *"could not write"* ]]
    [ ! -e "$STAGED" ]
}

@test "a staged guard that is not there fails" {
    rm -f "$STAGED"
    run bash "$SCRIPT" install "$STAGED"
    [ "$status" -eq 1 ]
}

@test "an unknown operation or option fails" {
    run bash "$SCRIPT" remove "$STAGED"
    [ "$status" -eq 1 ]
    run bash "$SCRIPT" install "$STAGED" --force
    [ "$status" -eq 1 ]
}
