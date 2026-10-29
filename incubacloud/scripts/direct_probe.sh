#!/usr/bin/env bash
# Check, from the host that publishes it, one direct name.
#
# Usage: direct_probe.sh <name> <ipv4> <port> <METHOD:PATH:STATUS[,STATUS...][:WORD]>...
#
# The probe of the direct path (plan-trafico-interno-directo, piece 3):
#
#   DNS     the name resolves to this host's address and nothing else, and
#           has no AAAA;
#   TLS     a handshake on <ipv4>:<port> validates the name's certificate
#           against the system's roots; its expiry is read too;
#   routes  each check, sent without credentials, answers its status, or
#           one of them when several are given (and its body holds WORD
#           when one is given): vmauth's 401, a JWKS's 200, and for
#           whatever the port does not serve 403 (``direct-deny``) or 421
#           (Traefik 2.11.47 and later refuses a connection negotiated with
#           the ``direct`` TLS options before ``direct-deny`` runs).
#
# The connection goes to <ipv4> whatever the name resolves to: a wrong
# record must not make this host test somebody else's server. DNS is
# judged on its own line.
#
# Prints one line per layer:
#   IC_DIRECT_DNS ok <address> | fail <why>
#   IC_DIRECT_TLS ok | fail <connect|tls> curl <exit code>
#   IC_DIRECT_CERT <notAfter, UTC, ISO 8601>
#   IC_DIRECT_ROUTE <METHOD> <PATH> <status> ok|fail
# Exits 0 whatever the probe found (the panel judges it), 3 when getent,
# curl or openssl is missing, 1 on arguments it refuses.
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 4 "$#" "direct_probe.sh <name> <ipv4> <port> <METHOD:PATH:STATUS[,STATUS...][:WORD]>..."

name="$1"
ip="$2"
port="$3"
shift 3
[[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] \
    || ic_die "not a host name: $name"
[[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || ic_die "not an IPv4 address: $ip"
[[ "$port" =~ ^[0-9]{4,5}$ ]] || ic_die "not a port: $port"
for check in "$@"; do
    [[ "$check" =~ ^(GET|POST):/[A-Za-z0-9/_.-]*:[0-9]{3}(,[0-9]{3})*(:[A-Za-z0-9_]+)?$ ]] \
        || ic_die "not a check: $check"
done

missing=()
for tool in getent curl openssl; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "IC_DIRECT_TOOLS missing ${missing[*]}"
    exit 3
fi

v4="$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ' | sed 's/ $//')"
v6="$(getent ahostsv6 "$name" 2>/dev/null | awk '{print $1}' | grep -v '^::ffff:' \
    | sort -u | tr '\n' ' ' | sed 's/ $//')"
if [ -z "$v4" ]; then
    echo "IC_DIRECT_DNS fail does not resolve"
elif [ "$v4" != "$ip" ]; then
    echo "IC_DIRECT_DNS fail resolves to $v4"
elif [ -n "$v6" ]; then
    echo "IC_DIRECT_DNS fail has an IPv6 record $v6"
else
    echo "IC_DIRECT_DNS ok $v4"
fi

url="https://$name:$port"
resolve=(--resolve "$name:$port:$ip")
curl -sS -o /dev/null --max-time 10 "${resolve[@]}" "$url/" 2>/dev/null
code=$?
case "$code" in
    0) echo "IC_DIRECT_TLS ok" ;;
    35|51|53|54|58|59|60|64|66|77|80|82|83|90|91) echo "IC_DIRECT_TLS fail tls curl $code" ;;
    *) echo "IC_DIRECT_TLS fail connect curl $code" ;;
esac
[ "$code" -eq 0 ] || exit 0

end="$(echo | openssl s_client -connect "$ip:$port" -servername "$name" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | sed -n 's/^notAfter=//p')"
if [ -n "$end" ]; then
    echo "IC_DIRECT_CERT $(date -u -d "$end" +%Y-%m-%dT%H:%M:%SZ)"
fi

for check in "$@"; do
    IFS=: read -r method path status word <<<"$check"
    body="$(mktemp)"
    got="$(curl -sS -o "$body" -w '%{http_code}' -X "$method" --max-time 10 \
        "${resolve[@]}" "$url$path" 2>/dev/null)"
    verdict=fail
    if [[ ",$status," == *",$got,"* ]] && { [ -z "$word" ] || grep -q "$word" "$body"; }; then
        verdict=ok
    fi
    rm -f "$body"
    echo "IC_DIRECT_ROUTE $method $path ${got:-000} $verdict"
done
