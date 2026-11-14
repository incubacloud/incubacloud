#!/usr/bin/env bats
# Tests for scripts/cron_guard.sh.
#
# ``docker`` is stubbed: the ``psql`` calls are answered from a file the
# test controls, so the pause loop can be driven through each of its
# outcomes without a database.
#
# Run locally with:  bats incubacloud/tests/shell

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/cron_guard.sh"
    TMP="$(mktemp -d)"
    STUB_DIR="${TMP}/bin"
    mkdir -p "${STUB_DIR}" "${TMP}/inst"
    PATH="${STUB_DIR}:${PATH}"
    export PATH TMP
    # How many active crons each 'SELECT count(*)' reports, one per line.
    echo "0" > "${TMP}/counts"
    _stub_docker
}

teardown() {
    rm -rf "${TMP}"
}

# A docker stub that logs every call and answers the count query from
# ${TMP}/counts, consuming one line per query.
_stub_docker() {
    cat > "${STUB_DIR}/docker" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "${TMP}/calls"
case "$*" in
    *"SELECT count(*)"*)
        head -1 "${TMP}/counts"
        sed -i '1d' "${TMP}/counts" 2>/dev/null || true
        ;;
    *"SELECT 1"*) echo 1 ;;
    *"string_agg"*) echo "Collect Host Metrics" ;;
    *"click-odoo-update"*) echo "update ran"; exit "${UPDATE_EXIT:-0}" ;;
esac
exit 0
EOF
    chmod +x "${STUB_DIR}/docker"
}

@test "runs the update once no cron is active" {
    run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"no cron is running"* ]]
    grep -q "click-odoo-update" "${TMP}/calls"
}

@test "waits for an in-flight cron and then updates" {
    printf '1\n0\n' > "${TMP}/counts"
    run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    [ "$status" -eq 0 ]
    [[ "$output" == *"still executing"* ]]
    grep -q "click-odoo-update" "${TMP}/calls"
}

@test "gives up with the retry code and never updates" {
    printf '1\n1\n1\n' > "${TMP}/counts"
    run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    [ "$status" -eq 75 ]
    [[ "$output" == *"Collect Host Metrics"* ]]
    ! grep -q "click-odoo-update" "${TMP}/calls"
}

@test "restores the crons it paused, even when the update fails" {
    UPDATE_EXIT=1 run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    [ "$status" -eq 1 ]
    grep -q "UPDATE ir_cron SET active = true" "${TMP}/calls"
}

@test "pauses without ever blocking on a running row" {
    run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    grep -q "SKIP LOCKED" "${TMP}/calls"
}

@test "rejects an unknown operation" {
    run bash "$SCRIPT" nonsense "${TMP}/inst" prod odoo
    [ "$status" -ne 0 ]
}

@test "the update runs out of Traefik and Sablier" {
    run bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1
    [ "$status" -eq 0 ]
    grep -Eq "compose run --label traefik.enable=false --label sablier.enable=false --name incubacloud-update-[0-9a-f]{12} --rm odoo click-odoo-update" "${TMP}/calls"
}

# A docker stub for a stop, that keeps what the database and the host
# would: the marker table (${TMP}/marker), crons paused (${TMP}/paused),
# the update's container (${TMP}/container, holding its name), a list
# dropped by a call that does not put the crons back
# (${TMP}/dropped_alone). Like the real client, which sets its own
# handlers, the slow call named by $1 ("rm" for the container's removal,
# "resume" for the crons') dies of a TERM even when the shell that runs it
# ignores TERM; its statements are then not applied.
_stub_docker_for_a_stop() {
    SLOW="$1"
    export SLOW
    cat > "${STUB_DIR}/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "${TMP}/calls"
slow() {
    [ -e "${TMP}/container.started" ] || return 0
    : > "${TMP}/slow"
    env --default-signal=TERM /bin/sleep 1 || exit 143
}
case "$*" in
    *"SELECT count(*)"*) echo 0 ;;
    *"SELECT 1"*) echo 1 ;;
    *"CREATE TABLE IF NOT EXISTS"*) : > "${TMP}/marker" ;;
    *"SET active = false"*) [ -e "${TMP}/marker" ] && : > "${TMP}/paused" ;;
    *"click-odoo-update"*)
        # compose run passes the TERM on to the container and leaves at
        # once; click-odoo-update, PID 1 there without a handler, goes on.
        name=""
        while [ "$#" -gt 0 ]; do
            [ "$1" = --name ] && name="$2"
            shift
        done
        printf '%s\n' "$name" > "${TMP}/container"
        : > "${TMP}/container.started"
        exec env --default-signal=TERM /bin/sleep 30 ;;
    "rm -f "*)
        [ "$SLOW" = rm ] && slow
        [ -e "${TMP}/container" ] && [ "$3" = "$(cat "${TMP}/container")" ] \
            && rm -f "${TMP}/container" ;;
    *"SET active = true"*)
        [ "$SLOW" = resume ] && slow
        [ -e "${TMP}/marker" ] && rm -f "${TMP}/paused"
        [[ "$*" == *"DROP TABLE IF EXISTS"* ]] && rm -f "${TMP}/marker" ;;
    *"DROP TABLE IF EXISTS"*) : > "${TMP}/dropped_alone"; rm -f "${TMP}/marker" ;;
esac
exit 0
STUB
    chmod +x "${STUB_DIR}/docker"
}

# Wait up to 5 s for file $1.
_wait_for() {
    for _ in $(seq 100); do
        [ -e "$1" ] && return 0
        /bin/sleep 0.05
    done
    return 1
}

# Run the guard as detached_step.sh runs a step (a session of its own,
# under ``timeout``), stop it once the update runs, and again once the
# slow call is under way: the stop's TERM to the session, then the one
# ``timeout`` passes on to its process group.
_stop_twice() {
    setsid timeout --kill-after=60 60 bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1 \
        >"${TMP}/out" 2>&1 </dev/null &
    pid=$!
    _wait_for "${TMP}/container.started"
    pkill -TERM -s "$pid"
    _wait_for "${TMP}/slow"
    pkill -TERM -s "$pid" || true
    status=0
    wait "$pid" || status=$?
}

@test "stopped as a detached step, TERM after TERM, it puts the crons back and drops their list together" {
    # The second TERM lands while the crons are put back: the docker
    # client of that call dies of it, and a list dropped by a call of its
    # own would leave the crons paused with nothing to restore them.
    _stub_docker_for_a_stop resume
    _stop_twice
    [ "$status" -eq 143 ]
    [ ! -e "${TMP}/paused" ]
    [ ! -e "${TMP}/marker" ]
    [ ! -e "${TMP}/dropped_alone" ]
}

@test "stopped as a detached step, it removes the update's container before it puts the crons back" {
    # compose run leaves on the TERM and the update would go on in its
    # container, with the crons running again. The second TERM lands
    # while the container is removed, and does not cut that either.
    _stub_docker_for_a_stop rm
    _stop_twice
    [ "$status" -eq 143 ]
    [ ! -e "${TMP}/container" ]
    [ ! -e "${TMP}/paused" ]
    [ ! -e "${TMP}/marker" ]
    rm_line="$(grep -n '^rm -f ' "${TMP}/calls" | cut -d: -f1)"
    resume_line="$(grep -n 'SET active = true' "${TMP}/calls" | tail -1 | cut -d: -f1)"
    [ -n "$rm_line" ] && [ "$rm_line" -lt "$resume_line" ]
}
