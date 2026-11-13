#!/usr/bin/env bats
# Tests for scripts/shared_guard.sh.
#
# ``iptables`` is a small fake over files, one per chain and one rule spec
# per line, so what the script leaves behind can be read back. ``docker``,
# ``id``, ``sudo``, ``systemctl``, ``logger`` and ``modprobe`` are stubbed;
# the install directory, the systemd directory, the state file, the lock,
# the sysctl and /sys/class/net are temporary ones.
#
# With FAKE_RACE set to a path, the first ``-A IC-SHARED -m mac`` starts
# a second ``apply`` (FAKE_RACE_SCRIPT) and gives it two seconds to run
# before the first one goes on: the install's apply and the timer's tick
# crossing on a chain not written yet.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/shared_guard.sh"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/bin" "$TMP/ipt" "$TMP/sys/br-0123456789ab"
    export IPT_DIR="$TMP/ipt" CALLS="$TMP/calls"
    : > "$CALLS"
    # Docker's chain, with the RETURN older releases append to it.
    printf '%s\n' "-j RETURN" > "$IPT_DIR/DOCKER-USER"
    printf '%s\n' "-j DOCKER-USER" "-j DOCKER-FORWARD" > "$IPT_DIR/FORWARD"
    export IC_SHARED_GUARD_DIR="$TMP/lib/incubacloud-shared-guard"
    export IC_SYSTEMD_DIR="$TMP/systemd"
    export IC_SHARED_GUARD_STATE="$TMP/state"
    export IC_SHARED_GUARD_LOCK="$TMP/lock"
    export IC_BRIDGE_NF_FILE="$TMP/bridge-nf-call-iptables"
    export IC_SYS_CLASS_NET="$TMP/sys"
    echo 1 > "$IC_BRIDGE_NF_FILE"

    cat > "$TMP/bin/iptables" <<'STUB'
#!/usr/bin/env bash
echo "iptables $*" >> "$CALLS"
args=("$@")
[ "${args[0]}" = -w ] && args=("${args[@]:2}")
cmd="${args[0]}"
chain="${args[1]:-}"
rest=("${args[@]:2}")
f="$IPT_DIR/$chain"
spec="${rest[*]}"
case "$cmd" in
    -S)
        [ -f "$f" ] || exit 1
        echo "-N $chain"
        while IFS= read -r rule; do echo "-A $chain $rule"; done < "$f"
        ;;
    -N) [ -f "$f" ] && exit 1; : > "$f" ;;
    -F) [ -f "$f" ] || exit 1; : > "$f" ;;
    -X) [ -f "$f" ] || exit 1; [ -s "$f" ] && exit 1; rm "$f" ;;
    -A)
        [ -f "$f" ] || exit 1
        [ -n "${FAKE_NO_MAC:-}" ] && [[ "$spec" == *"-m mac"* ]] && exit 1
        if [ -n "${FAKE_RACE:-}" ] && [ "$chain" = IC-SHARED ] \
            && [[ "$spec" == *"-m mac"* ]] && mkdir "$FAKE_RACE.started" 2>/dev/null; then
            ( exec 3>&- 9>&-
              bash "$FAKE_RACE_SCRIPT" apply > "$FAKE_RACE.out" 2>&1
              touch "$FAKE_RACE.done" ) &
            for _ in $(seq 20); do
                [ -e "$FAKE_RACE.done" ] && break
                sleep 0.1
            done
        fi
        echo "$spec" >> "$f"
        ;;
    -I)
        [ -f "$f" ] || exit 1
        spec="${rest[*]:1}"
        { echo "$spec"; cat "$f"; } > "$f.new" && mv "$f.new" "$f"
        ;;
    -C) [ -f "$f" ] || exit 1; grep -qxF -- "$spec" "$f" ;;
    -D)
        [ -f "$f" ] || exit 1
        grep -qxF -- "$spec" "$f" || exit 1
        grep -vxF -- "$spec" "$f" > "$f.new"; mv "$f.new" "$f"
        ;;
    *) exit 2 ;;
esac
STUB
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$1 $2" in
    "network inspect")
        [ -n "${FAKE_NO_NETWORK:-}" ] && exit 1
        case "$4" in
            *bridge.name*) echo "${FAKE_BRIDGE_NAME-<no value>}" ;;
            *.Id*) echo "${FAKE_NET_ID:-0123456789abcdef0123}" ;;
            *EnableIPv6*) echo "${FAKE_IPV6:-false}" ;;
        esac
        ;;
    "ps -q") [ -n "${FAKE_NO_PROXY:-}" ] || echo proxyid ;;
    "inspect -f") echo "${FAKE_PROXY_MAC:-02:69:63:00:00:01}" ;;
    "info --format") echo "${FAKE_BACKEND:-iptables}" ;;
esac
STUB
    cat > "$TMP/bin/id" <<'STUB'
#!/usr/bin/env bash
echo "${FAKE_UID:-0}"
STUB
    cat > "$TMP/bin/sudo" <<'STUB'
#!/usr/bin/env bash
[ "$1" = -n ] && shift
echo "sudo $*" >> "$CALLS"
FAKE_UID=0 exec "$@"
STUB
    cat > "$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS"
[ "$1" = is-active ] && echo active
exit 0
STUB
    cat > "$TMP/bin/logger" <<'STUB'
#!/usr/bin/env bash
echo "logger $*" >> "$CALLS"
STUB
    cat > "$TMP/bin/modprobe" <<'STUB'
#!/usr/bin/env bash
echo "modprobe $*" >> "$CALLS"
STUB
    chmod +x "$TMP"/bin/*
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    chmod -R u+w "$TMP" 2>/dev/null || true
    rm -rf "$TMP"
}

# The chain as the guard writes it.
expected_chain() {
    printf '%s\n' \
        "-m conntrack --ctstate RELATED,ESTABLISHED -j RETURN" \
        "-m mac --mac-source 02:69:63:00:00:01 -j RETURN" \
        "-j DROP"
}

@test "apply closes the network: the jump first, then the chain" {
    run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_ON bridge br-0123456789ab, proxy 02:69:63:00:00:01" ]]
    [ "$(cat "$IPT_DIR/IC-SHARED")" = "$(expected_chain)" ]
    [ "$(sed -n 1p "$IPT_DIR/DOCKER-USER")" = "-i br-0123456789ab -o br-0123456789ab -j IC-SHARED" ]
    # Docker's own rule stays, below.
    [ "$(sed -n 2p "$IPT_DIR/DOCKER-USER")" = "-j RETURN" ]
    [ ! -e "$IPT_DIR/IC-SHARED-PROBE" ]
}

@test "apply again writes nothing" {
    bash "$SCRIPT" apply
    before="$(cat "$IPT_DIR/DOCKER-USER" "$IPT_DIR/IC-SHARED")"
    : > "$CALLS"
    run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == IC_SHARED_GUARD_ON* ]]
    [ "$(cat "$IPT_DIR/DOCKER-USER" "$IPT_DIR/IC-SHARED")" = "$before" ]
    ! grep -Eq "iptables -w 5 -(A|I|F|N|D|X) " "$CALLS"
}

@test "a change of state is logged once" {
    bash "$SCRIPT" apply
    [ "$(grep -c '^logger ' "$CALLS")" -eq 1 ]
    grep -q "logger -t incubacloud-shared-guard -p user.notice IC_SHARED_GUARD_ON" "$CALLS"
    bash "$SCRIPT" apply
    [ "$(grep -c '^logger ' "$CALLS")" -eq 1 ]
    FAKE_PROXY_MAC=02:42:ac:14:00:02 bash "$SCRIPT" apply
    [ "$(grep -c '^logger ' "$CALLS")" -eq 2 ]
    grep -q "logger -t incubacloud-shared-guard -p user.warning IC_SHARED_GUARD_OPEN" "$CALLS"
}

@test "a proxy with another address opens the guard it had closed" {
    bash "$SCRIPT" apply
    FAKE_PROXY_MAC=02:42:AC:14:00:02 run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN the proxy's address on inverseproxy_shared is 02:42:ac:14:00:02, not 02:69:63:00:00:01: run Full Setup" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
    [ "$(cat "$IPT_DIR/DOCKER-USER")" = "-j RETURN" ]
}

@test "no proxy running: open" {
    bash "$SCRIPT" apply
    FAKE_NO_PROXY=1 run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN the proxy is not running on inverseproxy_shared" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
}

@test "no network, or no bridge: open" {
    FAKE_NO_NETWORK=1 run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN the network inverseproxy_shared is not there" ]]
    rmdir "$TMP/sys/br-0123456789ab"
    run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN the bridge br-0123456789ab of inverseproxy_shared is not there" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
}

@test "a firewall without the MAC match gets nothing" {
    FAKE_NO_MAC=1 run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_UNSUPPORTED the firewall has no MAC match (xt_mac)" ]]
    grep -q "modprobe xt_mac" "$CALLS"
    [ ! -e "$IPT_DIR/IC-SHARED" ]
    [ ! -e "$IPT_DIR/IC-SHARED-PROBE" ]
    [ "$(cat "$IPT_DIR/DOCKER-USER")" = "-j RETURN" ]
}

@test "no DOCKER-USER chain: nothing applied, nftables named" {
    rm "$IPT_DIR/DOCKER-USER"
    FAKE_BACKEND=nftables run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_UNSUPPORTED Docker writes its rules with nftables: there is no DOCKER-USER chain" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
}

@test "a DOCKER-USER that FORWARD does not jump to: open, nothing applied" {
    bash "$SCRIPT" apply
    printf '%s\n' "-j DOCKER-FORWARD" > "$IPT_DIR/FORWARD"
    run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN FORWARD does not jump to DOCKER-USER" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
    [ "$(cat "$IPT_DIR/DOCKER-USER")" = "-j RETURN" ]
    FAKE_BACKEND=nftables run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_UNSUPPORTED Docker writes its rules with nftables: FORWARD does not jump to DOCKER-USER" ]]
}

@test "two applies at once leave the chain whole" {
    FAKE_RACE="$TMP/race" FAKE_RACE_SCRIPT="$SCRIPT" run bash "$SCRIPT" apply
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == IC_SHARED_GUARD_ON* ]]
    [ -d "$TMP/race.started" ]
    for _ in $(seq 100); do
        [ -e "$TMP/race.done" ] && break
        sleep 0.1
    done
    [ -e "$TMP/race.done" ]
    [ "$(tail -n 1 "$TMP/race.out")" = "IC_SHARED_GUARD_ON bridge br-0123456789ab, proxy 02:69:63:00:00:01" ]
    [ "$(cat "$IPT_DIR/IC-SHARED")" = "$(expected_chain)" ]
    [ "$(grep -c 'IC-SHARED' "$IPT_DIR/DOCKER-USER")" -eq 1 ]
}

@test "a run that cannot take the lock leaves the rules alone" {
    bash "$SCRIPT" apply
    before="$(cat "$IPT_DIR/DOCKER-USER" "$IPT_DIR/IC-SHARED")"
    flock "$IC_SHARED_GUARD_LOCK" sleep 5 3>&- &
    holder=$!
    sleep 0.3
    : > "$CALLS"
    FAKE_PROXY_MAC=02:42:ac:14:00:02 IC_SHARED_GUARD_LOCK_WAIT=1 run bash "$SCRIPT" apply
    kill "$holder" 2>/dev/null || true
    [ "$status" -eq 1 ]
    [[ "$output" == *"another run of the guard kept its lock for 1s"* ]]
    [ "$(cat "$IPT_DIR/DOCKER-USER" "$IPT_DIR/IC-SHARED")" = "$before" ]
    ! grep -q "^iptables " "$CALLS"
}

@test "bridged packets that skip iptables: nothing applied" {
    echo 0 > "$IC_BRIDGE_NF_FILE"
    run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_UNSUPPORTED bridged packets do not reach iptables (bridge-nf-call-iptables is 0)" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
}

@test "a recreated network moves the jump to its new bridge" {
    bash "$SCRIPT" apply
    mkdir "$TMP/sys/br-fedcba987654"
    FAKE_NET_ID=fedcba9876543210 run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_ON bridge br-fedcba987654, proxy 02:69:63:00:00:01" ]]
    [ "$(grep -c 'IC-SHARED' "$IPT_DIR/DOCKER-USER")" -eq 1 ]
    grep -qxF -- "-i br-fedcba987654 -o br-fedcba987654 -j IC-SHARED" "$IPT_DIR/DOCKER-USER"
}

@test "a bridge named in the network's options is used" {
    mkdir "$TMP/sys/ic-shared0"
    FAKE_BRIDGE_NAME=ic-shared0 run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_ON bridge ic-shared0, proxy 02:69:63:00:00:01" ]]
}

@test "a chain someone changed is written again" {
    bash "$SCRIPT" apply
    printf '%s\n' "-j RETURN" > "$IPT_DIR/IC-SHARED"
    run bash "$SCRIPT" apply
    [[ "${lines[-1]}" == IC_SHARED_GUARD_ON* ]]
    [ "$(cat "$IPT_DIR/IC-SHARED")" = "$(expected_chain)" ]
}

@test "apply refuses to run without root" {
    FAKE_UID=1000 run bash "$SCRIPT" apply
    [ "$status" -eq 1 ]
    [[ "$output" == *"apply runs as root"* ]]
}

@test "install copies the guard, starts the minute timer and applies" {
    FAKE_UID=1000 run bash "$SCRIPT" install
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == IC_SHARED_GUARD_ON* ]]
    [ -f "$IC_SHARED_GUARD_DIR/shared_guard.sh" ]
    [ -f "$IC_SHARED_GUARD_DIR/lib/common.sh" ]
    grep -qxF "ExecStart=/bin/bash $IC_SHARED_GUARD_DIR/shared_guard.sh apply" \
        "$IC_SYSTEMD_DIR/incubacloud-shared-guard.service"
    grep -qxF "OnCalendar=*-*-* *:*:00" "$IC_SYSTEMD_DIR/incubacloud-shared-guard.timer"
    grep -q "systemctl enable --now incubacloud-shared-guard.timer" "$CALLS"
    # As root through sudo, from the setup's user.
    grep -qxF "sudo /bin/bash $IC_SHARED_GUARD_DIR/shared_guard.sh apply" "$CALLS"
    [ -e "$IPT_DIR/IC-SHARED" ]
}

@test "install without its timer leaves the guard open" {
    bash "$SCRIPT" apply
    mkdir -p "$TMP/lib"
    : > "$IC_SYSTEMD_DIR"
    run bash "$SCRIPT" install
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OPEN the incubacloud-shared-guard timer could not be installed" ]]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
}

@test "uninstall removes the timer, the files and the rules" {
    bash "$SCRIPT" install
    run bash "$SCRIPT" uninstall
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OFF removed" ]]
    grep -q "systemctl disable --now incubacloud-shared-guard.timer" "$CALLS"
    # A tick under way stopped before the rules go, after the timer.
    [ "$(grep -n "systemctl stop incubacloud-shared-guard.service" "$CALLS" | cut -d: -f1)" -gt \
      "$(grep -n "systemctl disable --now incubacloud-shared-guard.timer" "$CALLS" | cut -d: -f1)" ]
    [ ! -e "$IC_SYSTEMD_DIR/incubacloud-shared-guard.timer" ]
    [ ! -e "$IC_SYSTEMD_DIR/incubacloud-shared-guard.service" ]
    [ ! -e "$IC_SHARED_GUARD_DIR" ]
    [ ! -e "$IPT_DIR/IC-SHARED" ]
    [ "$(cat "$IPT_DIR/DOCKER-USER")" = "-j RETURN" ]
}

@test "uninstall on a host that never had it needs no root" {
    FAKE_UID=1000 run bash "$SCRIPT" uninstall
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_OFF not installed" ]]
    ! grep -q "^sudo " "$CALLS"
}

@test "status reads without writing" {
    bash "$SCRIPT" apply
    : > "$CALLS"
    run bash "$SCRIPT" status
    [ "$status" -eq 0 ]
    [[ "${lines[-1]}" == "IC_SHARED_GUARD_ON -i br-0123456789ab -o br-0123456789ab -j IC-SHARED" ]]
    ! grep -Eq "iptables -w 5 -(A|I|F|N|D|X) " "$CALLS"
}
