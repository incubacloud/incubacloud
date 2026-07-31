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

# Write a fake ``curl`` on PATH that prints $1 and exits with $2.
_stub_curl() {
    cat > "${STUB_DIR}/curl" <<EOF
#!/usr/bin/env bash
echo '$1'
exit ${2:-0}
EOF
    chmod +x "${STUB_DIR}/curl"
}

@test "succeeds as soon as Odoo's health payload answers" {
    _stub_curl '{"status": "pass"}'
    run bash "$SCRIPT" https://tenant.example.com 3 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"public URL is live"* ]]
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
