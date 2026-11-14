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
    grep -q "compose run --label traefik.enable=false --label sablier.enable=false --rm odoo click-odoo-update" "${TMP}/calls"
}

@test "stopped as a detached step does, TERM after TERM, it still resumes the crons" {
    # detached_step.sh stop sends TERM to the step's session, and the
    # step's ``timeout`` sends it again to its process group: one that
    # landed while the trap ran used to kill it there, with the crons
    # left paused. The update blocks until the first TERM, and the
    # resume is made slow so the second one lands inside the trap.
    cat > "${STUB_DIR}/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "${TMP}/calls"
case "$*" in
    *"SELECT count(*)"*) echo 0 ;;
    *"SELECT 1"*) echo 1 ;;
    *"click-odoo-update"*) : > "${TMP}/updating"; /bin/sleep 30 ;;
    *"SET active = true"*)
        [ -e "${TMP}/updating" ] && : > "${TMP}/resuming" && /bin/sleep 1 ;;
esac
exit 0
STUB
    chmod +x "${STUB_DIR}/docker"
    setsid timeout --kill-after=60 60 bash "$SCRIPT" update "${TMP}/inst" prod odoo 3 1 \
        >"${TMP}/out" 2>&1 </dev/null &
    pid=$!
    for _ in $(seq 100); do
        [ -e "${TMP}/updating" ] && break
        /bin/sleep 0.1
    done
    [ -e "${TMP}/updating" ]
    pkill -TERM -s "$pid"
    for _ in $(seq 100); do
        [ -e "${TMP}/resuming" ] && break
        /bin/sleep 0.05
    done
    [ -e "${TMP}/resuming" ]
    pkill -TERM -s "$pid" || true
    status=0
    wait "$pid" || status=$?
    [ "$status" -eq 143 ]
    update_line="$(grep -n 'click-odoo-update' "${TMP}/calls" | cut -d: -f1)"
    drop_line="$(grep -n 'DROP TABLE IF EXISTS incubacloud_cron_pause' "${TMP}/calls" | tail -1 | cut -d: -f1)"
    [ -n "$update_line" ] && [ "$drop_line" -gt "$update_line" ]
}
