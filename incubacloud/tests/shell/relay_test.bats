#!/usr/bin/env bats
# Tests for scripts/relay_test.sh.
#
# ``docker`` is stubbed on PATH: ``docker compose exec -T smtp <cmd>``
# runs the stub's version of the relay's commands, and its ``grep`` reads
# a mail log the test writes instead of /var/log/mail/mail.log. The log
# lines are docker-mailserver 16.0.1's, Amavis's hand-off on port 10024
# included.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/relay_test.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/inst" "$TMP/bin"
    export MAILLOG="$TMP/mail.log"
    : > "$MAILLOG"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    export IC_RELAY_TEST_POLL=0
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
# docker compose exec -T smtp <cmd...>
shift 4
echo "$*" >> "$CALLS"
case "$1" in
    ic-relay-oauth) [ -n "${TOKEN_JSON:-}" ] && printf '%s\n' "$TOKEN_JSON"; exit "${TOKEN_RC:-0}" ;;
    swaks) exit "${SWAKS_RC:-0}" ;;
    grep)
        shift
        args=()
        for a in "$@"; do
            [ "$a" = /var/log/mail/mail.log ] && a="$MAILLOG"
            args+=("$a")
        done
        exec grep "${args[@]}"
        ;;
    postsuper | postqueue) exit 0 ;;
esac
exit 99
STUB
    chmod +x "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

# log_mail TAG ID1 ID2 VERDICT_LINE_TAIL — the cleanup lines of both IDs,
# Amavis's hand-off, and the relay's line for ID2 ending with the tail.
log_mail() {
    local msgid="<ic-relay-test-$1@incubacloud.invalid>"
    cat >> "$MAILLOG" <<EOF
2026-11-10T10:00:00.000000+00:00 smtp postfix/cleanup[100]: $2: message-id=$msgid
2026-11-10T10:00:00.100000+00:00 smtp postfix/smtp[101]: $2: to=<me@example.org>, relay=127.0.0.1[127.0.0.1]:10024, delay=0.1, delays=0/0/0/0.1, dsn=2.0.0, status=sent (250 2.0.0 from MTA(smtp:[127.0.0.1]:10025): 250 2.0.0 Ok: queued as $3)
2026-11-10T10:00:00.200000+00:00 smtp postfix/cleanup[100]: $3: message-id=$msgid
EOF
    if [ -n "${4:-}" ]; then
        echo "2026-11-10T10:00:00.300000+00:00 smtp postfix/smtp[102]: $3: to=<me@example.org>, $4" >> "$MAILLOG"
    fi
}

run_test() {
    run bash "$SCRIPT" "$TMP/inst" relay@example.com me@example.org "$@"
}

@test "a sent mail says so, with the server's answer, and flushes the queue" {
    log_mail t1 AAAAAA1 BBBBBB2 \
        "relay=smtp.office365.com[52.0.0.1]:587, delay=0.2, delays=0/0/0.1/0.1, dsn=2.0.0, status=sent (250 2.0.0 OK [Hostname=X])"
    run_test t1 0 5
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = 'IC_RELAY_TEST {"status": "sent", "response": "250 2.0.0 OK [Hostname=X]", "token": null}' ]
    grep -q '^postqueue -f$' "$CALLS"
    ! grep -q '^postsuper' "$CALLS"
}

@test "the mail names itself and goes to the one recipient on port 25" {
    log_mail t1 AAAAAA1 BBBBBB2 "relay=h[1.2.3.4]:587, dsn=2.0.0, status=sent (250 ok)"
    run_test t1 0 5
    grep -q -- '^swaks --server 127.0.0.1:25 --from relay@example.com --to me@example.org ' "$CALLS"
    grep -q -- '--header Message-Id: <ic-relay-test-t1@incubacloud.invalid>' "$CALLS"
    grep -q -- '--header X-IC-Relay-Test: t1' "$CALLS"
}

@test "a deferred mail is removed under both of its IDs" {
    log_mail t2 AAAAAA1 BBBBBB2 \
        "relay=smtp.office365.com[52.0.0.1]:587, delay=0.1, dsn=4.7.3, status=deferred (SASL authentication failed; server smtp.office365.com[52.0.0.1] said: 535 5.7.3 Authentication unsuccessful)"
    run_test t2 0 5
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == *'"status": "deferred"'* ]]
    [[ "${lines[-1]}" == *'said: 535 5.7.3 Authentication unsuccessful'* ]]
    grep -q '^postsuper -d AAAAAA1$' "$CALLS"
    grep -q '^postsuper -d BBBBBB2$' "$CALLS"
    ! grep -q '^postqueue' "$CALLS"
}

@test "a refused connection is a verdict too (relay=none)" {
    log_mail t3 AAAAAA1 BBBBBB2 \
        "relay=none, delay=0.05, dsn=4.4.1, status=deferred (connect to smtp.example.com[192.0.2.5]:2525: Connection refused)"
    run_test t3 0 5
    [[ "${lines[-1]}" == *'"status": "deferred", "response": "connect to smtp.example.com[192.0.2.5]:2525: Connection refused"'* ]]
}

@test "Amavis's hand-off is not the relay's verdict" {
    log_mail t4 AAAAAA1 BBBBBB2 ""
    run_test t4 0 0
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = 'IC_RELAY_TEST {"status": "timeout", "response": "", "token": null}' ]
    grep -q '^postsuper -d BBBBBB2$' "$CALLS"
}

@test "no line at all within the wait is a timeout" {
    run_test t5 0 0
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = 'IC_RELAY_TEST {"status": "timeout", "response": "", "token": null}' ]
}

@test "the server's answer loses its addresses and stays one JSON string" {
    log_mail t6 AAAAAA1 BBBBBB2 \
        'relay=h[1.2.3.4]:587, dsn=5.2.252, status=bounced (host h[1.2.3.4] said: 554 5.2.252 SendAsDenied; relay@example.com not allowed to send as "Sales" <sales@example.org> (in reply to end of DATA command))'
    run_test t6 0 5
    [[ "${lines[-1]}" == *'"status": "bounced"'* ]]
    [[ "${lines[-1]}" == *'SendAsDenied; <address> not allowed to send as \"Sales\" <<address>>'* ]]
    [[ "${lines[-1]}" != *'example.'* ]]
}

@test "with OAuth2 the renewer's state comes along" {
    log_mail t7 AAAAAA1 BBBBBB2 "relay=h[1.2.3.4]:587, dsn=2.0.0, status=sent (250 ok)"
    export TOKEN_JSON='{"state": "ok", "expires_at": 1794179737}'
    run_test t7 1 5
    [ "${lines[-1]}" = 'IC_RELAY_TEST {"status": "sent", "response": "250 ok", "token": {"state": "ok", "expires_at": 1794179737}}' ]
    grep -q '^ic-relay-oauth status$' "$CALLS"
}

@test "an image without the renewer gives a null token, not a broken line" {
    log_mail t8 AAAAAA1 BBBBBB2 "relay=h[1.2.3.4]:587, dsn=2.0.0, status=sent (250 ok)"
    export TOKEN_RC=127
    run_test t8 1 5
    [[ "${lines[-1]}" == *'"token": null}' ]]
}

@test "a relay that does not take the mail fails the script" {
    export SWAKS_RC=24
    run_test t9 0 5
    [ "$status" -ne 0 ]
    [[ "$output" == *"did not take the test mail"* ]]
}

@test "a tag outside letters, digits and dashes is refused" {
    run_test 't;rm' 0 5
    [ "$status" -ne 0 ]
    [[ "$output" == *"invalid tag"* ]]
    ! grep -q swaks "$CALLS"
}
