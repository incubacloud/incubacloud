#!/usr/bin/env bats
# Tests for scripts/wait_public_url.sh.
#
# The script decides when an instance is "ready" for its own customer,
# so what matters is that it does NOT accept the panel's catch-all page
# (served with HTTP 200 while the instance's DNS record propagates) as
# an answer.
#
# Run locally with:  bats incubacloud/tests/shell
# CI runs the same command in the "Shell tests (bats)" job.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/wait_public_url.sh"
    STUB_DIR="$(mktemp -d)"
    PATH="${STUB_DIR}:${PATH}"
    export PATH
}

teardown() {
    rm -rf "${STUB_DIR}"
}

# Write a fake ``curl`` on PATH that records its arguments (one per
# line, in ${STUB_DIR}/args), prints $1 and exits with $2.
_stub_curl() {
    cat > "${STUB_DIR}/curl" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$@" > "${STUB_DIR}/args"
echo '$1'
exit ${2:-0}
EOF
    chmod +x "${STUB_DIR}/curl"
}

_VERSION_INFO='{"jsonrpc": "2.0", "id": null, "result": {"server_version": "19.0"}}'

@test "succeeds as soon as Odoo's version_info answers" {
    _stub_curl "$_VERSION_INFO"
    run bash "$SCRIPT" https://tenant.example.com 3 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"public URL is live"* ]]
}

@test "asks over a POST JSON-RPC call, which the CDN never challenges" {
    # A GET from the host gets the CDN's 403 challenge page, never the
    # instance: every claim timed out on it from 2026-09-22 on.
    _stub_curl "$_VERSION_INFO"
    run bash "$SCRIPT" https://tenant.example.com 1 1
    [ "$status" -eq 0 ]
    run cat "${STUB_DIR}/args"
    [[ "$output" == *$'-X\nPOST'* ]]
    [[ "$output" == *"https://tenant.example.com/web/webclient/version_info"* ]]
}

@test "rejects the catch-all 'being prepared' page and times out" {
    # The catch-all answers 200 with HTML — never a status payload.
    _stub_curl '<html><body>Your instance is being prepared</body></html>'
    run bash "$SCRIPT" https://tenant.example.com 2 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"did not answer"* ]]
}

@test "keeps polling while curl fails outright" {
    _stub_curl '' 7
    run bash "$SCRIPT" https://tenant.example.com 2 1
    [ "$status" -ne 0 ]
}

@test "requires its three arguments" {
    run bash "$SCRIPT" https://tenant.example.com
    [ "$status" -ne 0 ]
}
