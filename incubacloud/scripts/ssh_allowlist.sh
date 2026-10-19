#!/usr/bin/env bash
# Change who may open SSH on this host, live and for the next boot.
#
# Usage: ssh_allowlist.sh <add|remove|set> <ipv4[/len]>...
#
# The hardening playbook keeps the list in the named set
# ``inet filter ic_ssh_allow``. This replaces the whole set in one nft
# transaction (``flush`` + ``add element``: never an empty set, and no
# element-by-element delete that ``auto-merge`` could have folded away)
# and rewrites the set's ``elements`` line in /etc/nftables.conf, which is
# what the host loads at boot. The new file is parsed (``nft -c``) before
# anything changes; the live ruleset is changed first and the file only
# installed once that worked.
#
# A host hardened before the set existed has the list inlined in its SSH
# rule. The first change converts it: the set is created and the rule
# replaced by handle, live, and the file gets the same shape. Nothing
# else in the ruleset is touched -- the CDN sets are refreshed live by
# another job and the file's copy of them is stale on purpose.
#
# A change that would leave nobody allowed is refused. Prints the
# resulting list as ``IC_SSH_ALLOW <a, b, ...>``.
#
# IC_NFT_CONF overrides the ruleset path (tests).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "ssh_allowlist.sh <add|remove|set> <ipv4[/len]>..."

op="$1"
shift
conf="${IC_NFT_CONF:-/etc/nftables.conf}"
SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

case "$op" in
    add|remove|set) ;;
    *) ic_die "unknown operation: $op" ;;
esac
for ip in "$@"; do
    [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] \
        || ic_die "not an IPv4 address or network: $ip"
done
[ -f "$conf" ] || ic_die "no ruleset at $conf"

# Print the list the file holds now, one entry per line.
current_list() {
    if grep -q 'set ic_ssh_allow {' "$conf"; then
        awk '/set ic_ssh_allow \{/ {inset=1}
             inset && /elements = \{/ {
                 sub(/.*elements = \{/, ""); sub(/\}.*/, ""); print; exit }' "$conf"
    else
        grep -E 'tcp dport [0-9]+ ip saddr \{' "$conf" \
            | sed -E 's/.*ip saddr \{([^}]*)\}.*/\1/'
    fi | tr ',' '\n' | sed 's/[[:space:]]//g' | sed '/^$/d'
}

legacy_rules="$(grep -cE 'tcp dport [0-9]+ ip saddr \{' "$conf" || true)"
if ! grep -q 'set ic_ssh_allow {' "$conf" && [ "$legacy_rules" != "1" ]; then
    ic_die "cannot find the SSH allowlist in $conf ($legacy_rules inline rules)"
fi

mapfile -t old < <(current_list)
declare -a new=()
case "$op" in
    set) new=("$@") ;;
    add) new=("${old[@]}" "$@") ;;
    remove)
        for ip in "${old[@]}"; do
            keep=1
            for gone in "$@"; do [ "$ip" = "$gone" ] && keep=0; done
            [ "$keep" -eq 1 ] && new+=("$ip")
        done
        ;;
esac
# Dedup, keeping the order.
mapfile -t new < <(printf '%s\n' "${new[@]}" | awk 'NF && !seen[$0]++')
[ "${#new[@]}" -gt 0 ] || ic_die "refusing to leave nobody allowed to open SSH"
list="$(printf '%s, ' "${new[@]}")"
list="${list%, }"

# The file, with the new list.
staged="$(mktemp)"
trap 'rm -f "$staged"' EXIT
if grep -q 'set ic_ssh_allow {' "$conf"; then
    awk -v list="$list" '
        /set ic_ssh_allow \{/ {inset=1}
        inset && /elements = \{/ {
            sub(/elements = \{.*\}/, "elements = { " list " }"); inset=0 }
        { print }' "$conf" > "$staged"
else
    awk -v list="$list" '
        /tcp dport [0-9]+ ip saddr \{/ {
            sub(/ip saddr \{[^}]*\}/, "ip saddr @ic_ssh_allow") }
        { print }
        !done && /^[[:space:]]*table inet filter \{/ {
            print "    set ic_ssh_allow {"
            print "        type ipv4_addr"
            print "        flags interval"
            print "        auto-merge"
            print "        elements = { " list " }"
            print "    }"
            done=1 }' "$conf" > "$staged"
fi
$SUDO nft -c -f "$staged" || ic_die "the new ruleset does not parse; nothing changed"

# The live ruleset.
if $SUDO nft list set inet filter ic_ssh_allow >/dev/null 2>&1; then
    doc="flush set inet filter ic_ssh_allow
add element inet filter ic_ssh_allow { $list }
"
else
    # nft lists a one-address list without its braces.
    rule="$($SUDO nft -a list chain inet filter input \
        | grep -E 'tcp dport [0-9]+ ip saddr (\{|[0-9])' | head -n 1 || true)"
    [ -n "$rule" ] || ic_die "no live SSH rule to convert"
    handle="$(sed -E 's/.*# handle ([0-9]+).*/\1/' <<<"$rule")"
    port="$(sed -E 's/.*tcp dport ([0-9]+) .*/\1/' <<<"$rule")"
    ic_log "converting the inline SSH rule (handle $handle) to the named set"
    doc="add set inet filter ic_ssh_allow { type ipv4_addr; flags interval; auto-merge; }
add element inet filter ic_ssh_allow { $list }
replace rule inet filter input handle $handle tcp dport $port ip saddr @ic_ssh_allow accept
"
fi
printf '%s' "$doc" | $SUDO nft -f - || ic_die "the live ruleset refused the change"

$SUDO cp -p "$conf" "$conf.ic-prev"
$SUDO cp "$staged" "$conf"
ic_log "SSH allowlist ($op): $list"
echo "IC_SSH_ALLOW $list"
