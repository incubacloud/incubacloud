#!/usr/bin/env bash
# Keep the instances on the proxy's shared network from reaching each other.
#
# Usage: shared_guard.sh install|apply|uninstall|status
#
# Every instance's ``odoo`` joins ``inverseproxy_shared``, the network the
# host's Traefik reaches it on, and Docker lets any two containers of a
# network talk: one instance could open another's 8069 and 8072 directly,
# past the CDN and the proxy's routers. The guard is a chain of the
# host's firewall, jumped to from Docker's ``DOCKER-USER`` for what enters
# and leaves that network's bridge:
#
#   -A DOCKER-USER -i <bridge> -o <bridge> -j IC-SHARED
#   -A IC-SHARED -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
#   -A IC-SHARED -m mac --mac-source <proxy address> -j RETURN
#   -A IC-SHARED -j DROP
#
# Only the proxy opens connections there; the replies go back through
# conntrack. The proxy has a fixed address on the network
# (``mac_address`` in its compose file, ``PROXY_SHARED_MAC`` in the panel).
#
# It fails open. ``apply`` removes the chain, and says why, when the
# running proxy does not have that address (a proxy not recreated yet, a
# start Docker refused that brought the previous files back), when it is
# not running, when the network or its bridge is not there, and when the
# firewall cannot do it: no ``DOCKER-USER`` (Docker's nftables backend),
# no MAC match (``xt_mac``), a ``FORWARD`` that does not jump to
# ``DOCKER-USER`` (Docker switched to nftables and not restarted, or the
# chain made by another tool), or bridged packets that never reach
# iptables (``bridge-nf-call-iptables`` is not 1). A guard on another
# address would drop the proxy's own connections and cut every instance
# of the host.
#
# * ``install`` (Full Setup): copies this script and its library to
#   ``/usr/local/lib/incubacloud-shared-guard``, writes a systemd service
#   and a timer that runs ``apply`` every minute (the rules are lost at
#   every reboot, and a proxy recreated with another address must find the
#   guard open within a minute), then runs ``apply``. Without the timer it
#   applies nothing.
# * ``apply`` (the timer): idempotent; a rule is written only when missing.
#   One run at a time, under a lock: the one ``install`` starts and the
#   timer's could otherwise both rewrite the chain and leave a ``DROP``
#   first, which cuts the whole host until the next tick. A run that
#   waits too long for the lock exits 1 and touches nothing.
#   A change of state is written to the system log, tag
#   ``incubacloud-shared-guard``.
# * ``uninstall``: the timer, the files and the rules go; a run of the
#   timer under way is stopped first, so it cannot write them back.
# * ``status``: reads, changes nothing.
#
# Each operation ends with one line: ``IC_SHARED_GUARD_ON``,
# ``IC_SHARED_GUARD_OFF``, ``IC_SHARED_GUARD_OPEN <why>`` or
# ``IC_SHARED_GUARD_UNSUPPORTED <why>``. It exits 0 with any of them; 1
# only when it could not run (no root, an unknown operation).
#
# The IC_* variables below move the paths and names for the bats suite.
set -uo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

MAC="${IC_PROXY_SHARED_MAC:-02:69:63:00:00:01}"
NETWORK="${IC_SHARED_NETWORK:-inverseproxy_shared}"
PROXY_PROJECT="${IC_PROXY_PROJECT:-inverseproxy}"
CHAIN="IC-SHARED"
PROBE_CHAIN="IC-SHARED-PROBE"
INSTALL_DIR="${IC_SHARED_GUARD_DIR:-/usr/local/lib/incubacloud-shared-guard}"
SYSTEMD_DIR="${IC_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT="incubacloud-shared-guard"
STATE_FILE="${IC_SHARED_GUARD_STATE:-/run/incubacloud-shared-guard.state}"
LOCK_FILE="${IC_SHARED_GUARD_LOCK:-/run/incubacloud-shared-guard.lock}"
# Seconds a run waits for the one before it: an apply takes a few.
LOCK_WAIT="${IC_SHARED_GUARD_LOCK_WAIT:-30}"
# The first Compose that takes ``mac_address`` under a service's network.
COMPOSE_MAC_MIN=(2 24)
BRIDGE_NF="${IC_BRIDGE_NF_FILE:-/proc/sys/net/bridge/bridge-nf-call-iptables}"
SYS_NET="${IC_SYS_CLASS_NET:-/sys/class/net}"
LOG_TAG="incubacloud-shared-guard"

usage="shared_guard.sh install|apply|uninstall|status"
ic_require_args 1 "$#" "$usage"
op="$1"

# Run a command as root: directly when we are, through sudo otherwise.
# ``-n``: no terminal to ask a password on.
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo -n "$@"
    fi
}

# iptables, waiting for the lock Docker takes while it writes its rules.
ipt() {
    as_root iptables -w 5 "$@"
}

# Print the closing marker, and log a change of state to the system log.
# Arguments: the state (ON, OFF, OPEN, UNSUPPORTED), then the reason.
report() {
    local state="$1" reason="${2:-}" line previous=""
    line="IC_SHARED_GUARD_${state}${reason:+ $reason}"
    if [ "$(id -u)" -eq 0 ]; then
        previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
        if [ "$previous" != "$line" ]; then
            printf '%s\n' "$line" > "$STATE_FILE" 2>/dev/null || true
            case "$state" in
                ON|OFF) logger -t "$LOG_TAG" -p user.notice "$line" 2>/dev/null || true ;;
                *) logger -t "$LOG_TAG" -p user.warning "$line" 2>/dev/null || true ;;
            esac
        fi
    fi
    printf '%s\n' "$line"
}

# The jumps from DOCKER-USER to the chain, one rule spec per line
# (without ``-A DOCKER-USER``).
jumps() {
    ipt -S DOCKER-USER 2>/dev/null | while IFS= read -r rule; do
        case "$rule" in
            "-A DOCKER-USER "*"-j $CHAIN") printf '%s\n' "${rule#-A DOCKER-USER }" ;;
        esac
    done
}

# Delete one DOCKER-USER rule given its spec, as ``jumps`` prints it.
delete_jump() {
    local -a spec
    read -r -a spec <<<"$1"
    ipt -D DOCKER-USER "${spec[@]}" 2>/dev/null
}

# Remove the jumps, the chain and a probe chain left behind. Quiet when
# none of them is there.
remove_rules() {
    local rule
    while IFS= read -r rule; do
        [ -n "$rule" ] && delete_jump "$rule"
    done < <(jumps)
    for chain in "$CHAIN" "$PROBE_CHAIN"; do
        if ipt -S "$chain" >/dev/null 2>&1; then
            ipt -F "$chain" 2>/dev/null
            ipt -X "$chain" 2>/dev/null
        fi
    done
}

# The bridge device of the network: the name its options give, or
# Docker's default ``br-<first 12 characters of its id>``. Empty when the
# network is not there.
bridge_of_network() {
    local name id
    name="$(docker network inspect -f \
        '{{index .Options "com.docker.network.bridge.name"}}' "$NETWORK" 2>/dev/null)" \
        || return 0
    if [ -n "$name" ] && [ "$name" != "<no value>" ]; then
        printf '%s\n' "$name"
        return 0
    fi
    id="$(docker network inspect -f '{{.Id}}' "$NETWORK" 2>/dev/null)" || return 0
    [ -n "$id" ] && printf 'br-%s\n' "${id:0:12}"
}

# The addresses the running proxy containers have on the network, one per
# line, lower case. Nothing when no proxy runs.
proxy_macs() {
    local id
    docker ps -q \
        --filter "label=com.docker.compose.project=$PROXY_PROJECT" \
        --filter "label=com.docker.compose.service=proxy" 2>/dev/null \
        | while IFS= read -r id; do
            [ -n "$id" ] || continue
            docker inspect -f \
                "{{with index .NetworkSettings.Networks \"$NETWORK\"}}{{.MacAddress}}{{end}}" \
                "$id" 2>/dev/null | tr '[:upper:]' '[:lower:]'
        done
}

# Whether this firewall takes the MAC match: tried on a chain of its own,
# removed right after. Loads ``xt_mac`` first where it is a module.
mac_match_works() {
    local ok=1
    as_root modprobe xt_mac >/dev/null 2>&1 || true
    ipt -N "$PROBE_CHAIN" >/dev/null 2>&1 || true
    if ipt -A "$PROBE_CHAIN" -m mac --mac-source "$MAC" -j RETURN >/dev/null 2>&1; then
        ok=0
    fi
    ipt -F "$PROBE_CHAIN" >/dev/null 2>&1
    ipt -X "$PROBE_CHAIN" >/dev/null 2>&1
    return "$ok"
}

# Whether Docker writes its rules with nftables, where DOCKER-USER is not.
docker_uses_nftables() {
    local backend
    backend="$(docker info --format '{{.FirewallBackend.Driver}}' 2>/dev/null || true)"
    case "$backend" in
        nftables*) return 0 ;;
        *) return 1 ;;
    esac
}

# Hold the guard's lock for the rest of this run, waiting for a run under
# way. Exits 1 when it cannot: the rules are left as they are.
take_lock() {
    exec 9>>"$LOCK_FILE" || ic_die "could not open the lock $LOCK_FILE"
    flock -w "$LOCK_WAIT" 9 \
        || ic_die "another run of the guard kept its lock for ${LOCK_WAIT}s"
}

# Whether the chain holds exactly its three rules.
chain_is_current() {
    local count
    count="$(ipt -S "$CHAIN" 2>/dev/null | grep -c '^-A ')" || return 1
    [ "$count" = 3 ] || return 1
    ipt -C "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN 2>/dev/null \
        && ipt -C "$CHAIN" -m mac --mac-source "$MAC" -j RETURN 2>/dev/null \
        && ipt -C "$CHAIN" -j DROP 2>/dev/null
}

# Write the chain and its jump for BRIDGE. Fails when a rule is refused.
close_guard() {
    local bridge="$1" rule
    if ! chain_is_current; then
        ipt -N "$CHAIN" 2>/dev/null || true
        # Emptied first: until the DROP is back the chain lets everything
        # through, never the other way round.
        ipt -F "$CHAIN" || return 1
        ipt -A "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN || return 1
        ipt -A "$CHAIN" -m mac --mac-source "$MAC" -j RETURN || return 1
        ipt -A "$CHAIN" -j DROP || return 1
    fi
    # A jump for a bridge the network no longer has (recreated): dropped.
    while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        [ "$rule" = "-i $bridge -o $bridge -j $CHAIN" ] || delete_jump "$rule"
    done < <(jumps)
    if ! ipt -C DOCKER-USER -i "$bridge" -o "$bridge" -j "$CHAIN" 2>/dev/null; then
        ipt -I DOCKER-USER 1 -i "$bridge" -o "$bridge" -j "$CHAIN" || return 1
    fi
}

do_apply() {
    local bridge macs mac nf old
    if ! ipt -S DOCKER-USER >/dev/null 2>&1; then
        remove_rules
        if docker_uses_nftables; then
            report UNSUPPORTED "Docker writes its rules with nftables: there is no DOCKER-USER chain"
        else
            report UNSUPPORTED "there is no DOCKER-USER chain"
        fi
        return 0
    fi
    # A DOCKER-USER that FORWARD does not jump to filters nothing.
    if ! ipt -C FORWARD -j DOCKER-USER 2>/dev/null; then
        remove_rules
        if docker_uses_nftables; then
            report UNSUPPORTED "Docker writes its rules with nftables: FORWARD does not jump to DOCKER-USER"
        else
            report OPEN "FORWARD does not jump to DOCKER-USER"
        fi
        return 0
    fi
    bridge="$(bridge_of_network)"
    if [ -z "$bridge" ]; then
        remove_rules
        report OPEN "the network $NETWORK is not there"
        return 0
    fi
    if [ ! -e "$SYS_NET/$bridge" ]; then
        remove_rules
        report OPEN "the bridge $bridge of $NETWORK is not there"
        return 0
    fi
    nf="$(cat "$BRIDGE_NF" 2>/dev/null || true)"
    if [ "$nf" != 1 ]; then
        remove_rules
        report UNSUPPORTED "bridged packets do not reach iptables (bridge-nf-call-iptables is ${nf:-missing})"
        return 0
    fi
    macs="$(proxy_macs)"
    if [ -z "$macs" ]; then
        remove_rules
        report OPEN "the proxy is not running on $NETWORK"
        return 0
    fi
    while IFS= read -r mac; do
        if [ "$mac" != "${MAC,,}" ]; then
            remove_rules
            old="$(ic_compose_older_than "${COMPOSE_MAC_MIN[@]}")"
            if [ -n "$old" ]; then
                report UNSUPPORTED "Docker Compose $old cannot fix the proxy's address on $NETWORK (${COMPOSE_MAC_MIN[0]}.${COMPOSE_MAC_MIN[1]} or later)"
                return 0
            fi
            report OPEN "the proxy's address on $NETWORK is ${mac:-none}, not $MAC: run Full Setup"
            return 0
        fi
    done <<<"$macs"
    # The chain in place already proves the match works: probed only
    # before it is first written.
    if ! chain_is_current && ! mac_match_works; then
        remove_rules
        report UNSUPPORTED "the firewall has no MAC match (xt_mac)"
        return 0
    fi
    if ! close_guard "$bridge"; then
        remove_rules
        report OPEN "iptables refused a rule of the guard"
        return 0
    fi
    if [ "$(docker network inspect -f '{{.EnableIPv6}}' "$NETWORK" 2>/dev/null)" = true ]; then
        ic_warn "$NETWORK has IPv6, which this guard does not cover"
    fi
    report ON "bridge $bridge, proxy $MAC"
}

# Write the service and the timer. Fails when a unit cannot be written or
# the timer cannot be started.
install_units() {
    local service_tmp timer_tmp status=0
    service_tmp="$(mktemp)"
    timer_tmp="$(mktemp)"
    cat > "$service_tmp" <<UNIT
# Managed by IncubaCloud — rewritten on every Full Setup.
[Unit]
Description=Keep the instances on the proxy network from reaching each other
After=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_DIR/shared_guard.sh apply
# A run a minute: the state goes to the log only when it changes (logger,
# notice or warning), not each run's marker line or systemd's own lines.
StandardOutput=null
LogLevelMax=notice
UNIT
    cat > "$timer_tmp" <<UNIT
# Managed by IncubaCloud — rewritten on every Full Setup.
# The rules are lost at every reboot, and a proxy recreated with another
# address must find the guard open within a minute.
[Unit]
Description=Apply the guard on the proxy network every minute

[Timer]
OnCalendar=*-*-* *:*:00
AccuracySec=1s

[Install]
WantedBy=timers.target
UNIT
    if as_root mkdir -p "$SYSTEMD_DIR" \
        && as_root install -m 0644 "$service_tmp" "$SYSTEMD_DIR/$UNIT.service" \
        && as_root install -m 0644 "$timer_tmp" "$SYSTEMD_DIR/$UNIT.timer"; then
        as_root systemctl daemon-reload >/dev/null 2>&1 \
            && as_root systemctl enable --now "$UNIT.timer" >/dev/null 2>&1 \
            || status=1
    else
        status=1
    fi
    rm -f "$service_tmp" "$timer_tmp"
    return "$status"
}

do_install() {
    local here
    here="$(cd "$(dirname "$0")" && pwd)"
    if ! { as_root mkdir -p "$INSTALL_DIR/lib" \
        && as_root install -m 0755 "$here/shared_guard.sh" "$INSTALL_DIR/shared_guard.sh" \
        && as_root install -m 0644 "$here/lib/common.sh" "$INSTALL_DIR/lib/common.sh"; }; then
        remove_rules
        report OPEN "the guard could not be copied to $INSTALL_DIR"
        return 0
    fi
    if ! install_units; then
        # Without the timer nothing reopens the guard when the proxy's
        # address changes, nor applies it after a reboot.
        remove_rules
        report OPEN "the $UNIT timer could not be installed"
        return 0
    fi
    ic_log "timer $UNIT.timer enabled (every minute)"
    as_root /bin/bash "$INSTALL_DIR/shared_guard.sh" apply
}

do_uninstall() {
    if [ ! -e "$INSTALL_DIR" ] && [ ! -e "$SYSTEMD_DIR/$UNIT.timer" ] \
        && [ ! -e "$SYSTEMD_DIR/$UNIT.service" ]; then
        # Never installed: no rule of ours either, and no root needed.
        report OFF "not installed"
        return 0
    fi
    as_root systemctl disable --now "$UNIT.timer" >/dev/null 2>&1 || true
    # A run the timer started just before would write the rules back.
    as_root systemctl stop "$UNIT.service" >/dev/null 2>&1 || true
    as_root rm -f "$SYSTEMD_DIR/$UNIT.timer" "$SYSTEMD_DIR/$UNIT.service" \
        || ic_die "could not remove the $UNIT units"
    as_root systemctl daemon-reload >/dev/null 2>&1 || true
    remove_rules
    if [ -n "$(jumps)" ] || ipt -S "$CHAIN" >/dev/null 2>&1; then
        ic_die "the rules of the guard could not be removed"
    fi
    as_root rm -rf "$INSTALL_DIR" || ic_warn "could not remove $INSTALL_DIR"
    as_root rm -f "$STATE_FILE" 2>/dev/null || true
    report OFF "removed"
}

do_status() {
    local timer="not installed" jump
    if [ -e "$SYSTEMD_DIR/$UNIT.timer" ]; then
        timer="$(systemctl is-active "$UNIT.timer" 2>/dev/null || true)"
    fi
    ic_log "timer: ${timer:-unknown}"
    ic_log "bridge of $NETWORK: $(bridge_of_network)"
    ic_log "proxy address on $NETWORK: $(proxy_macs | tr '\n' ' ')(expected $MAC)"
    ic_log "last state: $(cat "$STATE_FILE" 2>/dev/null || echo none)"
    jump="$(jumps)"
    if [ -n "$jump" ] && chain_is_current; then
        printf 'IC_SHARED_GUARD_ON %s\n' "$jump"
    else
        printf 'IC_SHARED_GUARD_OFF no rules\n'
    fi
}

case "$op" in
    install) do_install ;;
    apply)
        [ "$(id -u)" -eq 0 ] || ic_die "apply runs as root"
        take_lock
        do_apply
        ;;
    uninstall) do_uninstall ;;
    status) do_status ;;
    *) ic_die "unknown operation: $op. Usage: $usage" ;;
esac
