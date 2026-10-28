#!/usr/bin/env bash
# Change who may open SSH on this host, live and for the next boot.
#
# Usage: ssh_allowlist.sh <add|remove|set> <address[/len]>...
#
# The hardening playbook keeps the list in two named sets, one per
# address family: ``inet filter ic_ssh_allow`` (IPv4) and
# ``ic_ssh_allow6`` (IPv6) -- a set holds one type of address, and nft
# refuses any other. This splits the list between them and replaces
# both whole in one nft transaction (``flush`` + ``add element``: no
# element-by-element delete that ``auto-merge`` could have folded away),
# and rewrites both sets in /etc/nftables.conf, which is what the host
# loads at boot. The new file is parsed (``nft -c``) before anything
# changes; the live ruleset is changed first and the file only installed
# once that worked. A family with no address keeps its set, empty.
#
# A host hardened before the sets existed has the list inlined in its
# SSH rule, and one hardened before 27-oct-2026 has the IPv4 set alone.
# The first change brings either to the current shape: the missing sets
# are created and the rules replaced or added by handle, live, and the
# file gets the same shape. Nothing else in the ruleset is touched --
# the CDN sets are refreshed live by another job and the file's copy of
# them is stale on purpose.
#
# A change that would leave nobody allowed is refused. Prints the
# resulting list as ``IC_SSH_ALLOW <a, b, ...>``.
#
# IC_NFT_CONF overrides the ruleset path (tests).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "ssh_allowlist.sh <add|remove|set> <address[/len]>..."

op="$1"
shift
conf="${IC_NFT_CONF:-/etc/nftables.conf}"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

case "$op" in
    add|remove|set) ;;
    *) ic_die "unknown operation: $op" ;;
esac
# Only the characters an address can hold reach nft; whether it is a
# valid one, nft decides when the file is parsed below. Entries are
# compared as text: lowercase only folds the case of IPv6, and
# ``2001:0db8::7`` is still not ``2001:db8::7`` here. The panel sends
# every entry compressed and keeps ``allowed_ssh_ips`` that way
# (``cloud.host._normalize_ssh_source``), so the lists the playbook
# wrote and the addresses this is given compare equal.
declare -a args=()
for ip in "$@"; do
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] \
        || [[ "$ip" == *:* && "$ip" =~ ^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$ ]] \
        || ic_die "not an IP address or network: $ip"
    args+=("${ip,,}")
done
[ -f "$conf" ] || ic_die "no ruleset at $conf"

# Print the elements of the set named $1 in the file, on one line.
set_elements() {
    awk -v name="$1" '
        $0 ~ "set " name " [{]" {inset=1; next}
        inset && /^[[:space:]]*[}][[:space:]]*$/ {exit}
        inset && /elements = [{]/ {
            sub(/.*elements = [{]/, ""); sub(/[}].*/, ""); print; exit }' "$conf"
}

# Print the list the file holds now, one entry per line.
current_list() {
    if grep -q 'set ic_ssh_allow {' "$conf"; then
        set_elements ic_ssh_allow
        set_elements ic_ssh_allow6
    else
        grep -E 'tcp dport [0-9]+ ip saddr \{' "$conf" \
            | sed -E 's/.*ip saddr \{([^}]*)\}.*/\1/'
    fi | tr ',' '\n' | sed 's/[[:space:]]//g' | sed '/^$/d' | tr 'A-F' 'a-f'
}

legacy_rules="$(grep -cE 'tcp dport [0-9]+ ip saddr \{' "$conf" || true)"
if ! grep -q 'set ic_ssh_allow {' "$conf" && [ "$legacy_rules" != "1" ]; then
    ic_die "cannot find the SSH allowlist in $conf ($legacy_rules inline rules)"
fi

mapfile -t old < <(current_list)
declare -a new=()
case "$op" in
    set) new=("${args[@]}") ;;
    add) new=("${old[@]}" "${args[@]}") ;;
    remove)
        for ip in "${old[@]}"; do
            keep=1
            for gone in "${args[@]}"; do [ "$ip" = "$gone" ] && keep=0; done
            [ "$keep" -eq 1 ] && new+=("$ip")
        done
        ;;
esac
# Dedup, keeping the order.
mapfile -t new < <(printf '%s\n' "${new[@]}" | awk 'NF && !seen[$0]++')
[ "${#new[@]}" -gt 0 ] || ic_die "refusing to leave nobody allowed to open SSH"
list="$(printf '%s, ' "${new[@]}")"
list="${list%, }"
declare -a v4=() v6=()
for ip in "${new[@]}"; do
    if [[ "$ip" == *:* ]]; then v6+=("$ip"); else v4+=("$ip"); fi
done
list4="$(printf '%s, ' "${v4[@]}")"
list4="${list4%, }"
list6="$(printf '%s, ' "${v6[@]}")"
list6="${list6%, }"

# The file, with the new list: both sets written anew -- in place of the
# IPv4 set, or first in the table on a host that had none -- and the
# IPv6 rule right after the IPv4 one.
staged="$(mktemp)"
trap 'rm -f "$staged"' EXIT
named=0
grep -q 'set ic_ssh_allow {' "$conf" && named=1
awk -v named="$named" -v l4="$list4" -v l6="$list6" '
    function sets(ind) {
        print ind "set ic_ssh_allow {"
        print ind "    type ipv4_addr"
        print ind "    flags interval"
        print ind "    auto-merge"
        if (l4 != "") print ind "    elements = { " l4 " }"
        print ind "}"
        print ind "set ic_ssh_allow6 {"
        print ind "    type ipv6_addr"
        print ind "    flags interval"
        print ind "    auto-merge"
        if (l6 != "") print ind "    elements = { " l6 " }"
        print ind "}"
    }
    skip { if ($0 ~ /^[[:space:]]*[}][[:space:]]*$/) skip = 0; next }
    /^[[:space:]]*set ic_ssh_allow6? [{]/ {
        if (!written) { ind = $0; sub(/set .*/, "", ind); sets(ind); written = 1 }
        skip = 1; next }
    /tcp dport [0-9]+ ip6 saddr @ic_ssh_allow6 / { next }
    /tcp dport [0-9]+ ip saddr [{]/ {
        sub(/ip saddr [{][^}]*[}]/, "ip saddr @ic_ssh_allow") }
    /tcp dport [0-9]+ ip saddr @ic_ssh_allow / {
        print
        rule6 = $0
        sub(/ip saddr @ic_ssh_allow /, "ip6 saddr @ic_ssh_allow6 ", rule6)
        print rule6
        next }
    { print }
    !named && !written && /^[[:space:]]*table inet filter [{]/ {
        sets("    "); written = 1 }' "$conf" > "$staged"
$SUDO nft -c -f "$staged" || ic_die "the new ruleset does not parse; nothing changed"

# The live ruleset, in one transaction.
doc=""
if $SUDO nft list set inet filter ic_ssh_allow >/dev/null 2>&1; then
    doc+="flush set inet filter ic_ssh_allow"$'\n'
    [ -z "$list4" ] || doc+="add element inet filter ic_ssh_allow { $list4 }"$'\n'
    # nft prints the rule without the set's braces.
    rule="$($SUDO nft -a list chain inet filter input \
        | grep -E 'tcp dport [0-9]+ ip saddr @ic_ssh_allow accept' | head -n 1 || true)"
    convert=""
else
    # nft lists a one-address list without its braces.
    rule="$($SUDO nft -a list chain inet filter input \
        | grep -E 'tcp dport [0-9]+ ip saddr (\{|[0-9])' | head -n 1 || true)"
    [ -n "$rule" ] || ic_die "no live SSH rule to convert"
    doc+="add set inet filter ic_ssh_allow { type ipv4_addr; flags interval; auto-merge; }"$'\n'
    [ -z "$list4" ] || doc+="add element inet filter ic_ssh_allow { $list4 }"$'\n'
    convert=1
fi
handle="$(sed -nE 's/.*# handle ([0-9]+).*/\1/p' <<<"$rule")"
port="$(sed -nE 's/.*tcp dport ([0-9]+) .*/\1/p' <<<"$rule")"
if $SUDO nft list set inet filter ic_ssh_allow6 >/dev/null 2>&1; then
    doc+="flush set inet filter ic_ssh_allow6"$'\n'
    [ -z "$list6" ] || doc+="add element inet filter ic_ssh_allow6 { $list6 }"$'\n'
else
    [ -n "$handle" ] || ic_die "no live SSH rule to add the IPv6 one after"
    ic_log "adding the IPv6 SSH set, its rule after handle $handle"
    doc+="add set inet filter ic_ssh_allow6 { type ipv6_addr; flags interval; auto-merge; }"$'\n'
    [ -z "$list6" ] || doc+="add element inet filter ic_ssh_allow6 { $list6 }"$'\n'
    # Added after the rule it follows before that rule is replaced: the
    # handle names the old rule until the transaction ends.
    doc+="add rule inet filter input position $handle tcp dport $port ip6 saddr @ic_ssh_allow6 accept"$'\n'
fi
if [ -n "$convert" ]; then
    ic_log "converting the inline SSH rule (handle $handle) to the named set"
    doc+="replace rule inet filter input handle $handle tcp dport $port ip saddr @ic_ssh_allow accept"$'\n'
fi
printf '%s' "$doc" | $SUDO nft -f - || ic_die "the live ruleset refused the change"

$SUDO cp -p "$conf" "$conf.ic-prev"
$SUDO cp "$staged" "$conf"
ic_log "SSH allowlist ($op): $list"
echo "IC_SSH_ALLOW $list"
