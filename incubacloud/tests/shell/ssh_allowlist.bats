#!/usr/bin/env bats
# Tests for scripts/ssh_allowlist.sh.
#
# ``nft`` and ``sudo`` are stubbed: the stub records what would change
# the live ruleset (``nft -f -`` documents land in $APPLIED) and answers
# the read-only queries from STUB_* variables. The script was also run
# against the real nft of Ubuntu 22.04 and Debian 13 (18-oct-2026), on
# rulesets rendered from the playbook before and after the named set.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/ssh_allowlist.sh"
    TMP="$(mktemp -d)"
    export IC_NFT_CONF="$TMP/nftables.conf"
    export APPLIED="$TMP/applied"
    export CHECKED="$TMP/checked"
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
exec "$@"
STUB
    cat > "$TMP/bin/nft" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "-c -f "*) cat "$3" > "$CHECKED"; exit "${STUB_CHECK_STATUS:-0}" ;;
    "list set inet filter ic_ssh_allow") exit "${STUB_LIVE_SET:-1}" ;;
    "-a list chain inet filter input")
        rule='tcp dport 22222 ip saddr { 203.0.113.7, 198.51.100.4 } accept # handle 7'
        printf '\t\t%s\n' "${STUB_LIVE_RULE:-$rule}"
        exit 0 ;;
    "-f -") cat > "$APPLIED"; exit "${STUB_APPLY_STATUS:-0}" ;;
esac
echo "unexpected nft $*" >&2
exit 2
STUB
    chmod +x "$TMP/bin/sudo" "$TMP/bin/nft"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    rm -rf "$TMP"
}

legacy_conf() {
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter
delete table inet filter

table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        tcp dport 22222 ip saddr { 203.0.113.7, 198.51.100.4 } accept
        tcp dport { 80, 443 } accept
    }
}
EOF
}

named_conf() {
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter
delete table inet filter

table inet filter {
    set ic_ssh_allow {
        type ipv4_addr
        flags interval
        auto-merge
        elements = { 203.0.113.7, 198.51.100.4 }
    }
    set ic_cdn_v4 {
        type ipv4_addr
        flags interval
        elements = { 192.0.2.0/24 }
    }
    chain input {
        type filter hook input priority filter; policy drop;
        tcp dport 22222 ip saddr @ic_ssh_allow accept
        tcp dport { 80, 443 } ip saddr @ic_cdn_v4 accept
    }
}
EOF
}

@test "a legacy host is converted to the named set, live and on disk" {
    legacy_conf
    run bash "$SCRIPT" add 77.42.42.201
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_SSH_ALLOW 203.0.113.7, 198.51.100.4, 77.42.42.201"* ]]
    grep -q "add set inet filter ic_ssh_allow { type ipv4_addr; flags interval; auto-merge; }" "$APPLIED"
    grep -q "add element inet filter ic_ssh_allow { 203.0.113.7, 198.51.100.4, 77.42.42.201 }" "$APPLIED"
    grep -q "replace rule inet filter input handle 7 tcp dport 22222 ip saddr @ic_ssh_allow accept" "$APPLIED"
    grep -q "tcp dport 22222 ip saddr @ic_ssh_allow accept" "$IC_NFT_CONF"
    grep -q "elements = { 203.0.113.7, 198.51.100.4, 77.42.42.201 }" "$IC_NFT_CONF"
    [ -f "$IC_NFT_CONF.ic-prev" ]
}

@test "a legacy host allowing one address is converted too" {
    # The file has the braces the playbook wrote; the live listing does
    # not (nft 1.0.2-1.1.3).
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        tcp dport 22222 ip saddr { 203.0.113.7 } accept
    }
}
EOF
    export STUB_LIVE_RULE="tcp dport 22222 ip saddr 203.0.113.7 accept # handle 2"
    run bash "$SCRIPT" add 77.42.42.201
    [ "$status" -eq 0 ]
    grep -q "replace rule inet filter input handle 2 tcp dport 22222 ip saddr @ic_ssh_allow accept" "$APPLIED"
    grep -q "add element inet filter ic_ssh_allow { 203.0.113.7, 77.42.42.201 }" "$APPLIED"
}

@test "a named set is replaced whole, and only its own line changes" {
    named_conf
    export STUB_LIVE_SET=0
    run bash "$SCRIPT" remove 198.51.100.4
    [ "$status" -eq 0 ]
    [ "$(cat "$APPLIED")" = "flush set inet filter ic_ssh_allow
add element inet filter ic_ssh_allow { 203.0.113.7 }" ]
    grep -q "elements = { 203.0.113.7 }" "$IC_NFT_CONF"
    # The CDN set's line is not the SSH set's.
    grep -q "elements = { 192.0.2.0/24 }" "$IC_NFT_CONF"
}

@test "adding an address already there changes nothing in the list" {
    named_conf
    export STUB_LIVE_SET=0
    run bash "$SCRIPT" add 203.0.113.7
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_SSH_ALLOW 203.0.113.7, 198.51.100.4"* ]]
}

@test "set replaces the list with exactly what it is given" {
    named_conf
    export STUB_LIVE_SET=0
    run bash "$SCRIPT" set 10.0.0.1 10.0.0.0/8
    [ "$status" -eq 0 ]
    grep -q "elements = { 10.0.0.1, 10.0.0.0/8 }" "$IC_NFT_CONF"
}

@test "nobody left allowed is refused before anything changes" {
    named_conf
    cp "$IC_NFT_CONF" "$TMP/before"
    run bash "$SCRIPT" remove 203.0.113.7 198.51.100.4
    [ "$status" -ne 0 ]
    [[ "$output" == *"refusing to leave nobody allowed"* ]]
    [ ! -f "$APPLIED" ]
    cmp -s "$TMP/before" "$IC_NFT_CONF"
}

@test "a value that is not an address is refused" {
    named_conf
    run bash "$SCRIPT" add '1.2.3.4;reboot'
    [ "$status" -ne 0 ]
    [[ "$output" == *"not an IPv4 address"* ]]
    [ ! -f "$APPLIED" ]
}

@test "a file that does not parse changes neither the host nor the file" {
    named_conf
    cp "$IC_NFT_CONF" "$TMP/before"
    export STUB_CHECK_STATUS=1
    run bash "$SCRIPT" add 77.42.42.201
    [ "$status" -ne 0 ]
    [ ! -f "$APPLIED" ]
    cmp -s "$TMP/before" "$IC_NFT_CONF"
}

@test "a live ruleset that refuses the change leaves the file alone" {
    named_conf
    cp "$IC_NFT_CONF" "$TMP/before"
    export STUB_LIVE_SET=0 STUB_APPLY_STATUS=1
    run bash "$SCRIPT" add 77.42.42.201
    [ "$status" -ne 0 ]
    cmp -s "$TMP/before" "$IC_NFT_CONF"
}

@test "a ruleset with no allowlist at all is refused" {
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter {
    chain input { tcp dport 22 accept }
}
EOF
    run bash "$SCRIPT" add 77.42.42.201
    [ "$status" -ne 0 ]
    [[ "$output" == *"cannot find the SSH allowlist"* ]]
}
