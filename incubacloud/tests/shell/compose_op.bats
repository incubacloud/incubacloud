#!/usr/bin/env bats
# Tests for scripts/compose_op.sh.
#
# ``docker`` is stubbed on PATH so the script's own logic is what gets
# exercised: argument handling, tilde expansion, the missing-directory
# rules and the exact compose command each operation builds. The stub
# answers the read-only queries the script makes (the stack's
# services, the running ones) from ``STUB_SERVICES`` / ``STUB_RUNNING``
# without recording them, so ``DOCKER_CALLS`` holds what changes state.
# The first ``STUB_CONFIG_FAILS`` reads of the services fail, and
# ``CONFIG_READS`` counts them all; with ``STUB_PS_FAILS`` set, the
# running ones cannot be listed. Odoo's routes go through the interfaces
# in ``STUB_ROUTED`` over IPv4 (eth0 unless set) and ``STUB_ROUTED6``
# over IPv6, besides the IPv6 link-local and multicast routes of those in
# ``STUB_LINK6`` and its loopback's; with ``STUB_EXEC_FAILS`` set they
# cannot be read, and with ``STUB_TIMEOUT`` set the read times out.
# ``STUB_STOP_FAILS`` makes a stop of the whole stack fail with 5.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/compose_op.sh"
    TMP="$(mktemp -d)"
    export HOME="$TMP/home"
    mkdir -p "$HOME/project/inst"

    # Stub docker: record the argv it was called with, succeed.
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "compose config --services")
        reads=$(( $(cat "$CONFIG_READS") + 1 ))
        echo "$reads" > "$CONFIG_READS"
        if [ "$reads" -le "${STUB_CONFIG_FAILS:-0}" ]; then
            echo "stub: cannot read the compose" >&2
            exit 1
        fi
        printf '%b' "${STUB_SERVICES:-}"; exit 0 ;;
    "compose ps -q --status running")
        if [ -n "${STUB_PS_FAILS:-}" ]; then
            echo "stub: cannot list the containers" >&2
            exit 1
        fi
        printf '%b' "${STUB_RUNNING:-}"; exit 0 ;;
    "compose exec -T odoo sh -c "*)
        if [ -n "${STUB_EXEC_FAILS:-}" ]; then
            echo "stub: cannot exec into odoo" >&2
            exit 1
        fi
        zero="00000000000000000000000000000000"
        printf 'Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'
        for iface in ${STUB_ROUTED-eth0}; do
            printf '%s\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n' "$iface"
        done
        route6() {
            printf '%s %s %s 00 %s 00000100 00000001 00000000 00000001 %8s\n' \
                "$1" "$2" "$zero" "$zero" "$3"
        }
        for iface in ${STUB_ROUTED6-}; do
            route6 fd5b1e6f7a0100000000000000000000 40 "$iface"
        done
        for iface in ${STUB_LINK6-}; do
            route6 fe800000000000000000000000000000 40 "$iface"
            route6 ff000000000000000000000000000000 08 "$iface"
        done
        route6 00000000000000000000000000000001 80 lo
        exit 0 ;;
    "compose stop")
        echo "docker $*" >> "$DOCKER_CALLS"
        [ -z "${STUB_STOP_FAILS:-}" ] || exit 5
        exit 0 ;;
    "inspect -f "*)
        # The ids are the service names; one ending in -run-1 is a
        # one-off, which the real format prints as an empty line.
        shift 3
        for id in "$@"; do
            case "$id" in *-run-1) echo ;; *) echo "$id" ;; esac
        done
        exit 0 ;;
esac
echo "docker $*" >> "$DOCKER_CALLS"
STUB
    chmod +x "$TMP/bin/docker"
    export DOCKER_CALLS="$TMP/calls"
    : > "$DOCKER_CALLS"
    export CONFIG_READS="$TMP/config_reads"
    echo 0 > "$CONFIG_READS"
    # The pause before a retried read is not what these tests wait for.
    printf '#!/usr/bin/env bash\n' > "$TMP/bin/sleep"
    chmod +x "$TMP/bin/sleep"
    # timeout runs the command, or gives up on it as at its deadline.
    cat > "$TMP/bin/timeout" <<'STUB'
#!/usr/bin/env bash
[ -z "${STUB_TIMEOUT:-}" ] || exit 124
shift
exec "$@"
STUB
    chmod +x "$TMP/bin/timeout"
    export PATH="$TMP/bin:$PATH"
}

# A staging with an allowlist: the egress sidecar is in the stack and
# odoo runs.
with_sidecar() {
    export STUB_SERVICES="odoo\ndb\nsmtp\nproxy_general\nodoo_net_setup\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\nproxy_general\nodoo_net_setup\n"
}

teardown() {
    rm -rf "$TMP"
}

@test "up starts the stack detached" {
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "stop without services stops the whole stack" {
    run bash "$SCRIPT" "$HOME/project/inst" stop
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop" ]
}

@test "stop with services stops only those" {
    run bash "$SCRIPT" "$HOME/project/inst" stop odoo backup
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop odoo backup" ]
}

@test "start starts existing containers" {
    run bash "$SCRIPT" "$HOME/project/inst" start
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start" ]
}

@test "start can target a single service" {
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
}

@test "restart stops and starts the whole stack" {
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start')" ]
}

@test "restart of named services still restarts only those" {
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp db
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose restart smtp db" ]
}

@test "a stop that fails still starts the stack, and fails the restart" {
    export STUB_STOP_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 5 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start')" ]
}

@test "down removes images, volumes and orphans" {
    run bash "$SCRIPT" "$HOME/project/inst" down
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose down --rmi all -v --remove-orphans" ]
}

@test "a tilde path resolves against HOME" {
    run bash "$SCRIPT" "~/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "down on a missing directory succeeds without calling docker" {
    run bash "$SCRIPT" "$HOME/project/gone" down
    [ "$status" -eq 0 ]
    [[ "$output" == *"nothing to shut down"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "up on a missing directory fails loudly" {
    run bash "$SCRIPT" "$HOME/project/gone" up
    [ "$status" -eq 1 ]
    [[ "$output" == *"instance directory not found"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "an unknown operation is refused" {
    run bash "$SCRIPT" "$HOME/project/inst" explode
    [ "$status" -eq 1 ]
    [[ "$output" == *"unknown operation"* ]]
    [ ! -s "$DOCKER_CALLS" ]
}

@test "missing arguments are refused with a usage line" {
    run bash "$SCRIPT" "$HOME/project/inst"
    [ "$status" -eq 1 ]
    [[ "$output" == *"Usage: compose_op.sh"* ]]
}

@test "a failing docker call fails the script" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
exit 3
STUB
    chmod +x "$TMP/bin/docker"
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 3 ]
}

@test "starting odoo restarts the egress sidecar after it" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo
docker compose restart odoo_net_setup" ]
}

@test "restarting the stack restarts the egress sidecar again, last" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop
docker compose start
docker compose restart odoo_net_setup" ]
}

@test "up restarts the egress sidecar once odoo runs" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d
docker compose restart odoo_net_setup" ]
}

@test "the sidecar is left alone when odoo is not running" {
    with_sidecar
    export STUB_RUNNING="db\n"
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
}

@test "a one-off odoo is not odoo running: the sidecar is left alone" {
    # ``compose ps --services --status running`` names a one-off after its
    # service from compose 2.29 on; restarting the sidecar then would be
    # for an odoo that does not run.
    with_sidecar
    export STUB_RUNNING="db\nodoo-run-1\n"
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "an operation that does not start odoo leaves the sidecar alone" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" start db
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start db" ]
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$HOME/project/inst" stop odoo
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop odoo" ]
}

@test "restarting the sidecar itself does not restart it twice" {
    with_sidecar
    run bash "$SCRIPT" "$HOME/project/inst" restart odoo_net_setup
    [ "$(cat "$DOCKER_CALLS")" = "docker compose restart odoo_net_setup" ]
}

# A staging rendered by template v10: odoo runs in gatekeeper's network.
with_gatekeeper() {
    export STUB_SERVICES="odoo\ndb\nsmtp\ngatekeeper\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\ngatekeeper\n"
}

# Odoo left in a namespace gatekeeper no longer has: Docker's interface
# and its routes are gone; a bridge gatekeeper made is still there, up,
# with nothing but its IPv6 link-local and multicast routes.
left_behind() {
    export STUB_ROUTED=""
    export STUB_LINK6="br9"
}

@test "restarting the stack stops and starts it, so odoo starts after gatekeeper" {
    # ``docker compose restart`` raced odoo against a gatekeeper not yet
    # running, and odoo was left exited, every time on Docker 29.
    with_gatekeeper
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start')" ]
}

@test "odoo left without a route is restarted into gatekeeper's network" {
    # A gatekeeper restarted, recreated or started again by ``up`` gets a
    # new namespace; a running odoo stays in the old one.
    with_gatekeeper
    left_behind
    for op in "start gatekeeper" "restart gatekeeper" "up" "start" "restart"; do
        : > "$DOCKER_CALLS"
        # shellcheck disable=SC2086  # the operation and its services
        run bash "$SCRIPT" "$HOME/project/inst" $op
        [ "$status" -eq 0 ]
        [[ "$output" == *"odoo has no route but its loopback"* ]]
        [ "$(tail -n 1 "$DOCKER_CALLS")" = "docker compose restart odoo" ]
        [ "$(grep -c 'restart odoo$' "$DOCKER_CALLS")" -eq 1 ]
    done
}

@test "interfaces with no route of their own do not count as a network" {
    # The kernel's tunnels (tunl0, sit0) in a new namespace are down and
    # have no route at all.
    with_gatekeeper
    export STUB_ROUTED=""
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose start gatekeeper\ndocker compose restart odoo')" ]
}

@test "odoo with a route is left alone, whatever the operation named" {
    with_gatekeeper
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

@test "a route over IPv6 alone is a network" {
    with_gatekeeper
    export STUB_ROUTED=""
    export STUB_ROUTED6="eth0"
    export STUB_LINK6="eth0"
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
}

@test "a stop or a down never restarts odoo" {
    # A stopped gatekeeper has no namespace to join.
    with_gatekeeper
    left_behind
    run bash "$SCRIPT" "$HOME/project/inst" stop gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose stop gatekeeper" ]
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$HOME/project/inst" down
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose down --rmi all -v --remove-orphans" ]
}

@test "odoo is left alone, and not asked, when it is not running" {
    with_gatekeeper
    export STUB_RUNNING="db\ngatekeeper\n"
    export STUB_EXEC_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
}

@test "a stop that failed in a restart still puts odoo in gatekeeper's network" {
    # The start ran: the steps after it run on what it brought up, and
    # the script ends with the stop's status.
    with_gatekeeper
    left_behind
    export STUB_STOP_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 5 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start\ndocker compose restart odoo')" ]
}

@test "a stack with neither gatekeeper nor sidecar gets nothing after the operation" {
    export STUB_SERVICES="odoo\ndb\nsmtp\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\n"
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start')" ]
    : > "$DOCKER_CALLS"
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
}

# ── a read that fails is not a stack without the step ──
#
# QA, walk 6f6: a read of the compose that failed would have been taken
# for a stack without the step, and nothing would have said odoo might be
# left with the wrong network. The operation itself has run: the script
# says what it could not check and ends with 24, which the executors take
# for a step done, with a warning.

@test "a compose read that fails once is retried, and the sidecar still follows odoo" {
    with_sidecar
    export STUB_CONFIG_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$status" -eq 0 ]
    [[ "$output" == *"retrying once"* ]]
    [ "$(cat "$CONFIG_READS")" -eq 2 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose start odoo\ndocker compose restart odoo_net_setup')" ]
}

@test "the gatekeeper step does not read the compose while its readings work" {
    with_gatekeeper
    left_behind
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 0 ]
    [ "$(cat "$CONFIG_READS")" -eq 0 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose start gatekeeper\ndocker compose restart odoo')" ]
}

@test "a start of gatekeeper that cannot read odoo's routes ends with 24 and says odoo may have no network" {
    with_gatekeeper
    export STUB_EXEC_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
    [[ "$output" == *"stub: cannot exec into odoo"* ]]
    [[ "$output" == *"could not read odoo's routes"* ]]
    [[ "$output" == *"WARNING: the start ran, but the steps that follow it could not be checked"* ]]
    [[ "$output" != *"ERROR"* ]]
    # The last line names what this operation may have left wrong, and
    # only that: a start of gatekeeper alone never touched odoo's route.
    last="$(tail -n 1 <<<"$output")"
    [[ "$last" == *"no network at all"* ]]
    [[ "$last" != *"unfiltered"* ]]
}

@test "a read of odoo's routes that times out is a read that failed" {
    with_gatekeeper
    export STUB_TIMEOUT=1
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
    [[ "$output" == *"could not read odoo's routes"* ]]
}

@test "a failed read on a stack whose compose shows no gatekeeper is not a warning" {
    # A production stack: the compose is read only once a reading has
    # failed, and settles whether there was anything to check.
    export STUB_SERVICES="odoo\ndb\nsmtp\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\n"
    export STUB_EXEC_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp
    [ "$status" -eq 0 ]
    [ "$(cat "$CONFIG_READS")" -eq 1 ]
    [[ "$output" != *"WARNING"* ]]
    unset STUB_EXEC_FAILS
    export STUB_PS_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp
    [ "$status" -eq 0 ]
    [[ "$output" != *"WARNING"* ]]
}

@test "a failed read on a stack whose compose shows a gatekeeper ends with 24" {
    with_gatekeeper
    export STUB_EXEC_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp
    [ "$status" -eq 24 ]
    [[ "$output" == *"could not read odoo's routes"* ]]
}

@test "a failed read on a stack whose compose cannot be read either ends with 24" {
    export STUB_SERVICES="odoo\ndb\nsmtp\n"
    export STUB_RUNNING="odoo\ndb\nsmtp\n"
    export STUB_EXEC_FAILS=1
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp
    [ "$status" -eq 24 ]
    [[ "$output" == *"stub: cannot read the compose"* ]]
    [[ "$output" == *"could not read odoo's routes"* ]]
}

@test "a start of odoo whose compose cannot be read ends with 24 and says its route may be unfiltered" {
    with_sidecar
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
    [[ "$output" == *"odoo_net_setup was not restarted after odoo"* ]]
    [[ "$output" == *"WARNING: the start ran"* ]]
    last="$(tail -n 1 <<<"$output")"
    [[ "$last" == *"unfiltered"* ]]
    [[ "$last" != *"no network"* ]]
}

@test "an up whose compose cannot be read ends with 24 once the stack is up" {
    with_sidecar
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" "$HOME/project/inst" up
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose up -d" ]
    [[ "$output" == *"odoo_net_setup was not restarted after odoo"* ]]
    [[ "$output" == *"WARNING: the up ran"* ]]
    [[ "$(tail -n 1 <<<"$output")" != *"no network"* ]]
}

@test "a restart of the stack that can read neither names both steps" {
    # Neither step knows which of the two egress models the stack has,
    # so the warning does not stop at the first and blame the wrong one.
    with_gatekeeper
    export STUB_CONFIG_FAILS=99
    export STUB_EXEC_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" restart
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "$(printf 'docker compose stop\ndocker compose start')" ]
    [[ "$output" == *"odoo_net_setup was not restarted after odoo"* ]]
    [[ "$output" == *"could not read odoo's routes"* ]]
    last="$(tail -n 1 <<<"$output")"
    [[ "$last" == *"unfiltered"* ]]
    [[ "$last" == *"no network at all"* ]]
}

@test "a start of gatekeeper that cannot tell whether odoo runs ends with 24" {
    # ``docker compose ps`` failing is not "odoo is not running".
    with_gatekeeper
    export STUB_PS_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" start gatekeeper
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start gatekeeper" ]
    [[ "$output" == *"stub: cannot list the containers"* ]]
    [[ "$output" == *"could not tell whether odoo runs, so its network was not checked"* ]]
    [[ "$(tail -n 1 <<<"$output")" == *"no network at all"* ]]
}

@test "a start of odoo that cannot tell whether odoo runs warns for the sidecar alone" {
    # Its compose shows the sidecar and no gatekeeper.
    with_sidecar
    export STUB_PS_FAILS=1
    run bash "$SCRIPT" "$HOME/project/inst" start odoo
    [ "$status" -eq 24 ]
    [ "$(cat "$DOCKER_CALLS")" = "docker compose start odoo" ]
    [[ "$output" == *"could not tell whether odoo runs, so odoo_net_setup was not restarted after it"* ]]
    [[ "$output" != *"its network was not checked"* ]]
    [[ "$(tail -n 1 <<<"$output")" != *"no network"* ]]
}

@test "an operation that does not start odoo does not read the compose" {
    export STUB_CONFIG_FAILS=99
    run bash "$SCRIPT" "$HOME/project/inst" restart smtp
    [ "$status" -eq 0 ]
    [ "$(cat "$CONFIG_READS")" -eq 0 ]
    run bash "$SCRIPT" "$HOME/project/inst" stop
    [ "$status" -eq 0 ]
    [ "$(cat "$CONFIG_READS")" -eq 0 ]
}
