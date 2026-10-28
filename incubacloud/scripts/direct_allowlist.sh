#!/usr/bin/env bash
# Admit the platform's addresses on this host's direct port, live and for
# the next boot.
#
# Usage: direct_allowlist.sh <set|clear> <port> [<ipv4[/len]>...]
#
# The direct port (cloud.host.direct_port) is a second Traefik entrypoint
# for the platform's own traffic. nftables admits on it only the named set
# ``inet filter ic_direct_v4``, through three rules at the end of the
# forward chain, where traffic DNAT'd to the proxy is judged (the CDN
# sets' reasoning in host_hardening.yml):
#
#   1. a new connection to the port from outside the set is dropped;
#   2. a source opening more than 20 new connections a second (burst 50)
#      is dropped: one Traefik serves this port and 443;
#   3. IPv6 on the port is dropped: the direct names have no AAAA.
#
# ``set`` replaces the whole set in one nft transaction (``flush`` + ``add
# element``) and adds the rules when they are missing or on another port;
# ``clear`` removes rules and set. Then /etc/nftables.conf gets the same
# shape, parsed with ``nft -c`` before it is installed, as ssh_allowlist.sh
# does. Every argument is checked before anything is touched: this is the
# panel's data reaching a root ruleset. An empty list is refused.
#
# Prints the live set as ``IC_DIRECT_ALLOW <a, b, ...>`` (auto-merge may
# show neighbouring addresses as one range), or ``IC_DIRECT_CLEARED``.
#
# IC_NFT_CONF overrides the ruleset path (tests).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "direct_allowlist.sh <set|clear> <port> [<ipv4[/len]>...]"

op="$1"
port="$2"
shift 2
conf="${IC_NFT_CONF:-/etc/nftables.conf}"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
MARK='comment "ic-direct"'

case "$op" in
    set|clear) ;;
    *) ic_die "unknown operation: $op" ;;
esac
if ! [[ "$port" =~ ^[0-9]{4,5}$ ]] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
    ic_die "not a direct port: $port"
fi
for cidr in "$@"; do
    [[ "$cidr" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(/([0-9]{1,2}))?$ ]] \
        || ic_die "not an IPv4 address or network: $cidr"
    prefix="${BASH_REMATCH[6]}"
    for octet in "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}" "${BASH_REMATCH[4]}"; do
        [ "$((10#$octet))" -le 255 ] || ic_die "not an IPv4 address or network: $cidr"
    done
    if [ -n "$prefix" ] && [ "$((10#$prefix))" -gt 32 ]; then
        ic_die "not an IPv4 address or network: $cidr"
    fi
done
if [ "$op" = set ] && [ "$#" -eq 0 ]; then
    ic_die "refusing an empty direct list: the port would admit nobody"
fi
[ -f "$conf" ] || ic_die "no ruleset at $conf"

list="$(printf '%s, ' "$@")"
list="${list%, }"
redirected="meta l4proto tcp ct state new ct status dnat ct original proto-dst $port"
rules=(
    "$redirected ip saddr != @ic_direct_v4 counter drop $MARK"
    "$redirected meter ic_direct_conn4 { ip saddr limit rate over 20/second burst 50 packets } counter drop $MARK"
    "meta nfproto ipv6 $redirected counter drop $MARK"
)

# The file: first without anything this script wrote, then, for ``set``,
# with the set after the table's opening and the rules before the forward
# chain's closing brace.
staged="$(mktemp)"
trap 'rm -f "$staged"' EXIT
awk '
    /set ic_direct_v4 \{/ { skip = 1 }
    skip { if ($0 ~ /^[[:space:]]*\}[[:space:]]*$/) { skip = 0 }; next }
    /comment "ic-direct"/ { next }
    { print }' "$conf" > "$staged"
if [ "$op" = set ]; then
    awk -v list="$list" -v r1="${rules[0]}" -v r2="${rules[1]}" -v r3="${rules[2]}" '
        !table && /^[[:space:]]*table inet filter \{/ {
            print
            print "    set ic_direct_v4 {"
            print "        type ipv4_addr"
            print "        flags interval"
            print "        auto-merge"
            print "        elements = { " list " }"
            print "    }"
            table = 1
            next
        }
        /^[[:space:]]*chain forward \{/ { forward = 1 }
        forward && /^[[:space:]]*\}[[:space:]]*$/ {
            print "        " r1
            print "        " r2
            print "        " r3
            forward = 0
            done = 1
        }
        { print }
        END { if (!table || !done) exit 3 }' "$staged" > "$staged.new" \
        || ic_die "cannot find the filter table and its forward chain in $conf"
    mv "$staged.new" "$staged"
fi
$SUDO nft -c -f "$staged" || ic_die "the new ruleset does not parse; nothing changed"

# The live ruleset, in one transaction.
live_set=0
if $SUDO nft list set inet filter ic_direct_v4 >/dev/null 2>&1; then
    live_set=1
fi
live_meter=0
if $SUDO nft list set inet filter ic_direct_conn4 >/dev/null 2>&1; then
    live_meter=1
fi
forward="$($SUDO nft -a list chain inet filter forward 2>/dev/null || true)"
handles="$(grep -F "$MARK" <<<"$forward" | sed -nE 's/.*# handle ([0-9]+).*/\1/p' || true)"
live_port="$(grep -F "$MARK" <<<"$forward" | sed -nE 's/.*proto-dst ([0-9]+) .*/\1/p' | head -n 1 || true)"
doc=""
if [ "$op" = set ]; then
    if [ "$live_set" -eq 1 ]; then
        doc+="flush set inet filter ic_direct_v4"$'\n'
    else
        doc+="add set inet filter ic_direct_v4 { type ipv4_addr; flags interval; auto-merge; }"$'\n'
    fi
    doc+="add element inet filter ic_direct_v4 { $list }"$'\n'
    if [ -z "$handles" ] || [ "$live_port" != "$port" ]; then
        for handle in $handles; do
            doc+="delete rule inet filter forward handle $handle"$'\n'
        done
        if [ -n "$handles" ] && [ "$live_meter" -eq 1 ]; then
            doc+="delete set inet filter ic_direct_conn4"$'\n'
        fi
        for rule in "${rules[@]}"; do
            doc+="add rule inet filter forward $rule"$'\n'
        done
    fi
else
    for handle in $handles; do
        doc+="delete rule inet filter forward handle $handle"$'\n'
    done
    if [ "$live_meter" -eq 1 ]; then
        doc+="delete set inet filter ic_direct_conn4"$'\n'
    fi
    if [ "$live_set" -eq 1 ]; then
        doc+="delete set inet filter ic_direct_v4"$'\n'
    fi
fi
if [ -n "$doc" ]; then
    printf '%s' "$doc" | $SUDO nft -f - || ic_die "the live ruleset refused the change"
fi

$SUDO cp -p "$conf" "$conf.ic-prev"
$SUDO cp "$staged" "$conf"
if [ "$op" = clear ]; then
    ic_log "direct list cleared"
    echo "IC_DIRECT_CLEARED"
    exit 0
fi
applied="$($SUDO nft list set inet filter ic_direct_v4 | tr -d '\n' \
    | sed -nE 's/.*elements = \{([^}]*)\}.*/\1/p' | tr -s ' \t' ' ' \
    | sed -E 's/^ //; s/ $//')"
ic_log "direct list on port $port: $applied"
echo "IC_DIRECT_ALLOW $applied"
