#!/usr/bin/env bats
# Tests for scripts/direct_probe.sh.
#
# ``getent``, ``curl`` and ``openssl`` are stubbed; the answers come from
# STUB_* variables, per path for curl.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/direct_probe.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/getent" <<'STUB'
#!/usr/bin/env bash
case "$1" in
    ahostsv4) printf '%s\n' "${STUB_V4-46.4.0.60      STREAM direct.metrics.example.test}" ;;
    ahostsv6) printf '%s\n' "${STUB_V6-::ffff:46.4.0.60 STREAM direct.metrics.example.test}" ;;
esac
STUB
    cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
out=""; fmt=""; url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift ;;
        -w) fmt="$2"; shift ;;
        -X|--max-time|--resolve) shift ;;
        https://*) url="$1" ;;
    esac
    shift
done
[ -n "${STUB_CURL_EXIT:-}" ] && exit "$STUB_CURL_EXIT"
case "$url" in
    */w/api/v1/write) code="${STUB_W:-401}"; body="" ;;
    */oauth/jwks) code="${STUB_JWKS:-200}"; body="${STUB_JWKS_BODY:-{\"keys\": [{\"kid\": \"k1\"}]}}" ;;
    *) code="${STUB_OTHER:-403}"; body="Forbidden" ;;
esac
[ -n "$out" ] && printf '%s' "$body" > "$out"
[ -n "$fmt" ] && printf '%s' "$code"
exit 0
STUB
    cat > "$TMP/bin/openssl" <<'STUB'
#!/usr/bin/env bash
case "$1" in
    s_client) cat >/dev/null; echo "CERT" ;;
    x509) cat >/dev/null; echo "notAfter=Jan 25 10:00:00 2027 GMT" ;;
esac
STUB
    chmod +x "$TMP/bin/getent" "$TMP/bin/curl" "$TMP/bin/openssl"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

probe() {
    run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 \
        POST:/w/api/v1/write:401 GET:/flags:403
}

@test "a name that works says so on every layer" {
    probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_DIRECT_DNS ok 46.4.0.60"* ]]
    [[ "$output" == *"IC_DIRECT_TLS ok"* ]]
    [[ "$output" == *"IC_DIRECT_CERT 2027-01-25T10:00:00Z"* ]]
    [[ "$output" == *"IC_DIRECT_ROUTE POST /w/api/v1/write 401 ok"* ]]
    [[ "$output" == *"IC_DIRECT_ROUTE GET /flags 403 ok"* ]]
}

@test "a name still on the CDN fails DNS" {
    STUB_V4=$'104.21.0.1 STREAM x\n172.67.0.1 STREAM x' probe
    [[ "$output" == *"IC_DIRECT_DNS fail resolves to 104.21.0.1 172.67.0.1"* ]]
}

@test "an AAAA record fails DNS" {
    STUB_V6="2606:4700::1 STREAM x" probe
    [[ "$output" == *"IC_DIRECT_DNS fail has an IPv6 record 2606:4700::1"* ]]
}

@test "a certificate nobody trusts fails TLS and asks no route" {
    STUB_CURL_EXIT=60 probe
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_DIRECT_TLS fail tls curl 60"* ]]
    [[ "$output" != *"IC_DIRECT_ROUTE"* ]]
}

@test "a closed port fails the connection" {
    STUB_CURL_EXIT=7 probe
    [[ "$output" == *"IC_DIRECT_TLS fail connect curl 7"* ]]
}

@test "a route that answers something else fails" {
    STUB_OTHER=200 probe
    [[ "$output" == *"IC_DIRECT_ROUTE GET /flags 200 fail"* ]]
}

@test "a refusal check takes 403 or 421, and only those" {
    for code in 403 421; do
        STUB_OTHER=$code run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 GET:/flags:403,421
        [ "$status" -eq 0 ]
        [[ "$output" == *"IC_DIRECT_ROUTE GET /flags $code ok"* ]]
    done
    for code in 200 404 4210 42; do
        STUB_OTHER=$code run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 GET:/flags:403,421
        [[ "$output" == *"IC_DIRECT_ROUTE GET /flags $code fail"* ]]
    done
}

@test "a check with one status takes that one only" {
    STUB_OTHER=421 probe
    [[ "$output" == *"IC_DIRECT_ROUTE GET /flags 421 fail"* ]]
}

@test "a JWKS without keys fails its check" {
    STUB_JWKS_BODY='{}' run bash "$SCRIPT" direct.www.example.test 46.4.0.60 9443 GET:/oauth/jwks:200:keys
    [[ "$output" == *"IC_DIRECT_ROUTE GET /oauth/jwks 200 fail"* ]]
}

@test "a missing tool fails the step and says which" {
    mkdir -p "$TMP/nocurl"
    for tool in getent openssl; do cp "$TMP/bin/$tool" "$TMP/nocurl/"; done
    for tool in awk sort tr sed grep mktemp date dirname cat rm; do
        ln -s "$(command -v "$tool")" "$TMP/nocurl/$tool"
    done
    PATH="$TMP/nocurl" run "$BASH" "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 GET:/flags:403
    [ "$status" -eq 3 ]
    [[ "$output" == *"IC_DIRECT_TOOLS missing curl"* ]]
}

@test "arguments that are not what they say are refused" {
    run bash "$SCRIPT" 'direct.metrics.example.test;id' 46.4.0.60 9443 GET:/flags:403
    [ "$status" -ne 0 ]
    run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 'GET:/flags;id:403'
    [ "$status" -ne 0 ]
    run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 94a3 GET:/flags:403
    [ "$status" -ne 0 ]
    for check in 'GET:/flags:403,' 'GET:/flags:,421' 'GET:/flags:403,42' 'GET:/flags:403;421'; do
        run bash "$SCRIPT" direct.metrics.example.test 46.4.0.60 9443 "$check"
        [ "$status" -ne 0 ]
    done
}
