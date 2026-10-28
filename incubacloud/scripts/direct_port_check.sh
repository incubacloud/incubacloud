#!/usr/bin/env bash
# Say whether the direct port is free for the proxy, before Full Setup
# recreates it.
#
# Usage: direct_port_check.sh <port>
#
# A port can be taken in two places and both are read: what Docker
# publishes, on any address, and what listens on the host. With
# ``userland-proxy: false`` (Cloud1's daemon) a published port is a DNAT
# rule and no socket at all, so reading sockets alone would miss it, and
# the proxy recreated onto it would not start. The proxy itself
# publishing the port is a second Full Setup, not a conflict, and so is
# the ``docker-proxy`` socket a host with the userland proxy keeps for a
# published port: whose port that is, Docker's list already said.
#
# Prints ``IC_DIRECT_PORT_FREE``, or ``IC_DIRECT_PORT_BUSY <who>`` and
# exits 1.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 1 "$#" "direct_port_check.sh <port>"

port="$1"
if ! [[ "$port" =~ ^[0-9]{4,5}$ ]] || [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
    ic_die "not a direct port: $port"
fi
proxy="${IC_PROXY_CONTAINER:-inverseproxy-proxy-1}"
SS=(ss)
[ "$(id -u)" -eq 0 ] || SS=(sudo -n ss)

busy=()
while read -r name ports; do
    [ -n "$name" ] || continue
    [ "$name" = "$proxy" ] && continue
    if grep -qE "(^|[ ,])[^ ,]*:${port}->" <<<"$ports"; then
        busy+=("container $name")
    fi
done < <(docker ps --format '{{.Names}} {{.Ports}}')
while read -r line; do
    [ -n "$line" ] || continue
    case "$line" in
        *docker-proxy*) continue ;;
    esac
    busy+=("process ${line##* }")
done < <("${SS[@]}" -Hltnp "sport = :$port" 2>/dev/null || true)

if [ "${#busy[@]}" -gt 0 ]; then
    echo "IC_DIRECT_PORT_BUSY ${busy[*]}"
    exit 1
fi
echo "IC_DIRECT_PORT_FREE"
