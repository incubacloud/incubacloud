#!/usr/bin/env bats
# Tests for scripts/direct_allowlist.sh.
#
# ``nft`` and ``sudo`` are stubbed as in ssh_allowlist.bats: what would
# change the live ruleset (``nft -f -``) lands in $APPLIED, the file
# parsed by ``nft -c`` in $CHECKED, and the read-only queries answer from
# STUB_* variables. The script was also run against the real nft of
# Ubuntu 22.04 and Debian 13 (plan, task 4, step 5).

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/direct_allowlist.sh"
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
    "list set inet filter ic_direct_v4")
        if [ -s "$APPLIED" ] || [ "${STUB_LIVE_SET:-1}" = 0 ]; then
            printf '%s\n' "${STUB_SET_LISTING:-table inet filter { set ic_direct_v4 { elements = { 10.0.0.0/8, 46.4.0.20 } } }}"
            exit 0
        fi
        exit 1 ;;
    "list set inet filter ic_direct_conn4") exit "${STUB_LIVE_METER:-1}" ;;
    "-a list chain inet filter forward") printf '%s\n' "${STUB_FORWARD:-}"; exit 0 ;;
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

# The shape the hardening playbook writes today, behind the CDN.
current_conf() {
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter
delete table inet filter

table inet filter {
    set ic_ssh_allow {
        type ipv4_addr
        flags interval
        auto-merge
        elements = { 203.0.113.7 }
    }
    set ic_cdn_v4 {
        type ipv4_addr
        flags interval
        elements = { 192.0.2.0/24 }
    }
    chain input {
        type filter hook input priority filter; policy drop;
        tcp dport 22222 ip saddr @ic_ssh_allow accept
    }
    chain forward {
        type filter hook forward priority filter; policy accept;
        meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } ip saddr @ic_cdn_v4 counter accept
        meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } counter drop
    }
    chain output {
        type filter hook output priority filter; policy accept;
    }
}
EOF
}

# Cloud1's: hardened before the SSH set existed, the list inlined.
legacy_conf() {
    cat > "$IC_NFT_CONF" <<'EOF'
table inet filter
delete table inet filter

table inet filter {
    set ic_cdn_v4 {
        type ipv4_addr
        flags interval
        elements = { 192.0.2.0/24 }
    }
    chain input {
        type filter hook input priority filter; policy drop;
        tcp dport 22222 ip saddr { 203.0.113.7, 198.51.100.4 } accept
    }
    chain forward {
        type filter hook forward priority filter; policy accept;
        meta l4proto tcp ct state new ct status dnat ct original proto-dst { 80, 443 } counter drop
    }
}
EOF
}

live_rules() {
    # What ``nft -a list chain`` prints of rules the script added on $1.
    printf '\t\tmeta l4proto tcp ct state new ct status dnat ct original proto-dst %s ip saddr != @ic_direct_v4 counter packets 0 bytes 0 drop comment "ic-direct" # handle 12\n' "$1"
    printf '\t\tmeta l4proto tcp ct state new ct status dnat ct original proto-dst %s meter ic_direct_conn4 { ip saddr limit rate over 20/second burst 50 packets } counter packets 0 bytes 0 drop comment "ic-direct" # handle 13\n' "$1"
    printf '\t\tmeta nfproto ipv6 meta l4proto tcp ct state new ct status dnat ct original proto-dst %s counter packets 0 bytes 0 drop comment "ic-direct" # handle 14\n' "$1"
}

@test "set creates the set and the three rules at the end of the forward chain" {
    current_conf
    run bash "$SCRIPT" set 9443 10.0.0.0/8 46.4.0.20/32
    [ "$status" -eq 0 ]
    grep -q 'set ic_direct_v4 {' "$IC_NFT_CONF"
    grep -q 'elements = { 10.0.0.0/8, 46.4.0.20/32 }' "$IC_NFT_CONF"
    [ "$(grep -c 'comment "ic-direct"' "$IC_NFT_CONF")" -eq 3 ]
    # After the CDN's drop, before the chain closes, in order.
    awk '/chain forward/{f=1} f{print} f&&/^    }/{exit}' "$IC_NFT_CONF" > "$TMP/forward"
    run grep -n 'ic-direct\|{ 80, 443 } counter drop' "$TMP/forward"
    [[ "${lines[0]}" == *"{ 80, 443 } counter drop"* ]]
    [[ "${lines[1]}" == *"ip saddr != @ic_direct_v4 counter drop"* ]]
    [[ "${lines[2]}" == *"meter ic_direct_conn4 { ip saddr limit rate over 20/second burst 50 packets } counter drop"* ]]
    [[ "${lines[3]}" == *"meta nfproto ipv6"*"proto-dst 9443 counter drop"* ]]
    grep -q '^add set inet filter ic_direct_v4 { type ipv4_addr; flags interval; auto-merge; }$' "$APPLIED"
    grep -q '^add element inet filter ic_direct_v4 { 10.0.0.0/8, 46.4.0.20/32 }$' "$APPLIED"
    [ "$(grep -c '^add rule inet filter forward ' "$APPLIED")" -eq 3 ]
}

@test "set works on Cloud1's older ruleset too" {
    legacy_conf
    run bash "$SCRIPT" set 9443 10.0.0.0/8 46.4.0.20/32
    [ "$status" -eq 0 ]
    grep -q 'set ic_direct_v4 {' "$IC_NFT_CONF"
    [ "$(grep -c 'comment "ic-direct"' "$IC_NFT_CONF")" -eq 3 ]
    # The inline SSH rule is not this script's business.
    grep -q 'tcp dport 22222 ip saddr { 203.0.113.7, 198.51.100.4 } accept' "$IC_NFT_CONF"
}

@test "a new list replaces the whole set and leaves rules on the same port alone" {
    current_conf
    bash "$SCRIPT" set 9443 10.0.0.0/8 46.4.0.20/32
    rm -f "$APPLIED"
    export STUB_LIVE_SET=0 STUB_FORWARD="$(live_rules 9443)"
    run bash "$SCRIPT" set 9443 10.0.0.0/8 46.4.0.21/32
    [ "$status" -eq 0 ]
    grep -q 'elements = { 10.0.0.0/8, 46.4.0.21/32 }' "$IC_NFT_CONF"
    [ "$(grep -c '46.4.0.20' "$IC_NFT_CONF" || true)" -eq 0 ]
    [ "$(grep -c 'comment "ic-direct"' "$IC_NFT_CONF")" -eq 3 ]
    [ "$(sed -n 1p "$APPLIED")" = "flush set inet filter ic_direct_v4" ]
    grep -q '^add element inet filter ic_direct_v4 { 10.0.0.0/8, 46.4.0.21/32 }$' "$APPLIED"
    [ "$(grep -c 'add rule\|delete rule' "$APPLIED" || true)" -eq 0 ]
}

@test "another port replaces the rules and their meter" {
    current_conf
    bash "$SCRIPT" set 9443 10.0.0.0/8
    rm -f "$APPLIED"
    export STUB_LIVE_SET=0 STUB_LIVE_METER=0 STUB_FORWARD="$(live_rules 9443)"
    run bash "$SCRIPT" set 9444 10.0.0.0/8
    [ "$status" -eq 0 ]
    for handle in 12 13 14; do
        grep -q "^delete rule inet filter forward handle $handle$" "$APPLIED"
    done
    grep -q '^delete set inet filter ic_direct_conn4$' "$APPLIED"
    [ "$(grep -c '^add rule inet filter forward .*proto-dst 9444 ' "$APPLIED")" -eq 3 ]
    [ "$(grep -c 'proto-dst 9443' "$IC_NFT_CONF" || true)" -eq 0 ]
}

@test "an empty list, a bad address or a bad port touches nothing" {
    current_conf
    cp "$IC_NFT_CONF" "$TMP/before"
    run bash "$SCRIPT" set 9443
    [ "$status" -ne 0 ]
    for bad in 1.2.3.256 1.2.3.4/33 '1.2.3.4;flush ruleset' 2001:db8::1 10.0.0.0/8x; do
        run bash "$SCRIPT" set 9443 10.0.0.0/8 "$bad"
        [ "$status" -ne 0 ]
    done
    for port in 80 1023 65536 94a3 ''; do
        run bash "$SCRIPT" set "$port" 10.0.0.0/8
        [ "$status" -ne 0 ]
    done
    [ ! -e "$APPLIED" ]
    cmp -s "$TMP/before" "$IC_NFT_CONF"
}

@test "a file nft refuses changes nothing live" {
    current_conf
    cp "$IC_NFT_CONF" "$TMP/before"
    STUB_CHECK_STATUS=1 run bash "$SCRIPT" set 9443 10.0.0.0/8
    [ "$status" -ne 0 ]
    [ ! -e "$APPLIED" ]
    cmp -s "$TMP/before" "$IC_NFT_CONF"
}

@test "clear removes the set and the rules" {
    current_conf
    bash "$SCRIPT" set 9443 10.0.0.0/8
    rm -f "$APPLIED"
    export STUB_LIVE_SET=0 STUB_LIVE_METER=0 STUB_FORWARD="$(live_rules 9443)"
    run bash "$SCRIPT" clear 9443
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_DIRECT_CLEARED"* ]]
    [ "$(grep -c 'ic_direct\|ic-direct' "$IC_NFT_CONF" || true)" -eq 0 ]
    grep -q '^delete rule inet filter forward handle 12$' "$APPLIED"
    grep -q '^delete set inet filter ic_direct_conn4$' "$APPLIED"
    grep -q '^delete set inet filter ic_direct_v4$' "$APPLIED"
}

@test "it prints what the live set holds, merged ranges included" {
    current_conf
    export STUB_SET_LISTING="$(printf 'table inet filter {\n\tset ic_direct_v4 {\n\t\telements = { 10.0.0.0/8, 46.4.0.20-46.4.0.21,\n\t\t\t     127.0.0.0/8 }\n\t}\n}')"
    run bash "$SCRIPT" set 9443 10.0.0.0/8 46.4.0.20/32 46.4.0.21/32 127.0.0.0/8
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_DIRECT_ALLOW 10.0.0.0/8, 46.4.0.20-46.4.0.21, 127.0.0.0/8"* ]]
}
