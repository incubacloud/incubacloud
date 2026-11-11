#!/usr/bin/env bats
# A restore to a moment cancelled in each of its phases, against a real
# PostgreSQL in Docker (the stack's own image): pitr.sh restore-start,
# restore-run and restore-cancel as they run on a host.
#
# wal-g and the agent are test doubles in the instance's .walg/: base
# copies are pg_basebackup tars, the archive a directory, so the
# database really is fetched, swapped in and recovered to the moment.
# Each double leaves a mark in marks/ when it starts and can be slowed
# down, which is how a cancel lands in the phase it is meant for.
#
# Slow (a stack per test) and needs Docker: skipped unless
# IC_PITR_DOCKER_TESTS=1.
#   IC_PITR_DOCKER_TESTS=1 bats incubacloud_pitr/tests/shell/pitr_restore_docker.bats

PG_IMAGE="${IC_PITR_PG_IMAGE:-ghcr.io/tecnativa/postgres-autoconf:17-alpine}"
BASE=base_000000010000000000000002

setup() {
    [ "${IC_PITR_DOCKER_TESTS:-}" = "1" ] || skip "set IC_PITR_DOCKER_TESTS=1 to run it against Docker"
    command -v docker >/dev/null || skip "no docker"
    SRC="${BATS_TEST_DIRNAME}/../../scripts"
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/scripts/lib"
    cp "$SRC"/*.sh "$TMP/scripts/"
    cp "${BATS_TEST_DIRNAME}/../../../incubacloud/scripts/lib/common.sh" "$TMP/scripts/lib/"
    cp "${BATS_TEST_DIRNAME}/../../../incubacloud/scripts/cron_guard.sh" "$TMP/scripts/"
    SCRIPT="$TMP/scripts/pitr.sh"
    DIR="$TMP/inst"
    JOB="$((RANDOM + 1000))"
    export COMPOSE_PROJECT_NAME="icpitrtest$JOB"
    mkdir -p "$DIR/.walg" "$DIR/.docker" "$DIR/archive" "$DIR/bases" "$DIR/marks" "$DIR/bin"
    chmod 777 "$DIR/archive" "$DIR/marks"
    echo "IC_PITR_PREFIX=test" > "$DIR/.docker/pitr.env"
    # No network: everything goes through docker exec and the database's
    # socket, and a host whose address pools are all taken still runs it.
    cat > "$DIR/docker-compose.yml" <<EOF
services:
  db:
    image: $PG_IMAGE
    network_mode: none
    environment:
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: odoo
      POSTGRES_DB: prod
    volumes:
      - db:/var/lib/postgresql/data
      - ./.walg:/opt/walg:ro
      - ./archive:/archive
      - ./bases:/bases:ro
      - ./marks:/marks
  odoo:
    image: alpine:3
    network_mode: none
    command: ["sleep", "infinity"]
    # PID 1 ignores TERM: a stop takes the whole grace period.
    stop_grace_period: 6s
    volumes:
      - ./bin/click-odoo-update:/usr/local/bin/click-odoo-update:ro
      - ./marks:/marks
  pitr:
    image: alpine:3
    network_mode: none
    command: ["sleep", "infinity"]
    stop_grace_period: 1s
    volumes:
      - ./.walg:/opt/walg:ro
      - ./marks:/marks
volumes:
  db:
EOF
    # wal-g: a base copy is a tar, a segment a file of archive/.
    cat > "$DIR/.walg/run" <<'EOF'
#!/bin/sh
case "$1" in
    backup-fetch)
        touch /marks/fetching
        [ -f /marks/delay-fetch ] && sleep "$(cat /marks/delay-fetch)"
        tar -xf "/bases/$3.tar" -C "$2" ;;
    wal-fetch)
        touch /marks/wal-fetch
        [ -f /marks/delay-wal ] && sleep "$(cat /marks/delay-wal)"
        [ -f "/archive/$2" ] || exit 1
        cp "/archive/$2" "$3" ;;
    *) exit 1 ;;
esac
EOF
    cat > "$DIR/.walg/pitr_agent.sh" <<'EOF'
#!/bin/sh
case "$1" in
    fs-restore)
        touch /marks/fs-restore
        [ -f /marks/delay-fs ] && sleep "$(cat /marks/delay-fs)"
        touch /marks/fs-restored ;;
esac
EOF
    # The module update: it notes when it starts and ends, can be slowed
    # down (delay-update) or made to fail (fail-update).
    cat > "$DIR/bin/click-odoo-update" <<'EOF'
#!/bin/sh
touch /marks/update
[ -f /marks/delay-update ] && sleep "$(cat /marks/delay-update)"
if [ -f /marks/fail-update ]; then
    echo "ERROR prod odoo.modules.loading: module broken_module failed"
    exit 1
fi
touch /marks/updated
EOF
    chmod 755 "$DIR/.walg/run" "$DIR/.walg/pitr_agent.sh" "$DIR/bin/click-odoo-update"
    cd "$DIR"
    docker compose up -d --quiet-pull >/dev/null 2>&1
    # The image's first start initialises the cluster on a server of its
    # own, which answers too, and then restarts it.
    for _ in $(seq 1 120); do
        docker compose logs db 2>/dev/null | grep -q "init process complete" && break
        sleep 1
    done
    wait_db
    sql_admin "ALTER SYSTEM SET archive_mode = 'on'"
    # marks/hold-archive holds every segment back: a drain then waits.
    sql_admin "ALTER SYSTEM SET archive_command = 'test ! -f /marks/hold-archive && test ! -f /archive/%f && cp %p /archive/%f'"
    docker compose restart db >/dev/null 2>&1
    wait_db
    sql "CREATE TABLE t (v text)"
    # What the update's guard pauses and puts back.
    sql "CREATE TABLE ir_cron (id serial PRIMARY KEY, cron_name text, active boolean)"
    sql "INSERT INTO ir_cron (cron_name, active) VALUES ('mail queue', true), ('off', false)"
    sql "INSERT INTO t VALUES ('1-base')"
    docker compose exec -T db pg_basebackup -U odoo -D - -Ft -X fetch > "$DIR/bases/$BASE.tar"
    sql "INSERT INTO t VALUES ('2-before-target')"
    sleep 2
    TARGET="$(date -u '+%Y-%m-%d %H:%M:%S')+00"
    sleep 2
    # The data as it is when the restore starts: what a cancel puts back.
    sql "INSERT INTO t VALUES ('3-after-target')"
    sql_admin "SELECT pg_switch_wal()" >/dev/null
}

teardown() {
    [ -n "${DIR:-}" ] || return 0
    cd "$DIR" || return 0
    # IC_PITR_KEEP=1 keeps the stack and its files, to look at a failure.
    if [ -z "${IC_PITR_KEEP:-}" ]; then
        # A restore a failed test left running: its holds let go, its
        # process group (a session of its own) gone.
        rm -f "$DIR/marks/hold-"*
        pid="$(cat "$DIR/.walg/restore/pid" 2>/dev/null || true)"
        [ -n "$pid" ] && kill -KILL -- "-$pid" 2>/dev/null || true
        docker rm -f "pitr-recovery-$JOB" "pitr-fetch-$JOB" >/dev/null 2>&1 || true
        # One-off containers too: one still running keeps the volume.
        docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
            | xargs -r docker rm -f >/dev/null 2>&1 || true
        docker compose down -v -t 1 >/dev/null 2>&1 || true
        # Written by the containers' users.
        docker run --rm -v "$TMP:/t" alpine:3 rm -rf /t/inst >/dev/null 2>&1 || true
        rm -rf "$TMP"
    fi
}

# psql against the instance's database.
sql() {
    docker compose exec -T db psql -U odoo -d prod -v ON_ERROR_STOP=1 -Atqc "$1"
}

sql_admin() {
    docker compose exec -T db psql -U odoo -d postgres -v ON_ERROR_STOP=1 -Atqc "$1"
}

wait_db() {
    for _ in $(seq 1 60); do
        docker compose exec -T db pg_isready -U odoo >/dev/null 2>&1 && return 0
        sleep 1
    done
    echo "the database did not come up" >&2
    return 1
}

# Wait up to $2 s (default 60) for the mark $1.
wait_mark() {
    for _ in $(seq 1 "${2:-60}"); do
        [ -e "$DIR/marks/$1" ] && return 0
        sleep 1
    done
    echo "no mark $1; log:" >&2
    cat "$DIR/.walg/restore/log" >&2
    return 1
}

# Wait up to $2 s (default 120) for the restore log to say $1.
wait_log() {
    for _ in $(seq 1 "${2:-120}"); do
        grep -q "$1" "$DIR/.walg/restore/log" 2>/dev/null && return 0
        sleep 1
    done
    echo "the log never said '$1':" >&2
    cat "$DIR/.walg/restore/log" >&2
    return 1
}

# Put a docker of the test's before the real one, for the restore's
# process and everything it starts. It holds a phase while its mark is
# there: once the fetch's container ended (hold-after-fetch, the copy
# fetched whole and nothing swapped in yet), and as the promoted
# database is stopped to come up as the instance's (hold-promoting).
wrap_docker() {
    REAL_DOCKER="$(command -v docker)"
    export REAL_DOCKER MARKS="$DIR/marks"
    mkdir -p "$TMP/bin"
    cat > "$TMP/bin/docker" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    "stop -t 120 pitr-recovery-"*)
        touch "$MARKS/promoting"
        while [ -e "$MARKS/hold-promoting" ]; do sleep 0.2; done ;;
esac
rc=0
"$REAL_DOCKER" "$@" || rc=$?
case "$*" in
    *backup-fetch*)
        touch "$MARKS/fetch-ended"
        while [ -e "$MARKS/hold-after-fetch" ]; do sleep 0.2; done ;;
esac
exit "$rc"
EOF
    chmod 755 "$TMP/bin/docker"
    export PATH="$TMP/bin:$PATH"
}

# What the data volume holds at its top, read with the database stopped.
volume_top() {
    "${REAL_DOCKER:-docker}" run --rm --network none \
        -v "${COMPOSE_PROJECT_NAME}_db:/d:ro" alpine:3 ls -a /d
}

# Start the restore; $1 1 asks for the update of its modules.
start_restore() {
    bash "$SCRIPT" restore-start "$DIR" odoo "$JOB" 7698 "$BASE" 1 "$TARGET" "$(date -u +%F)" \
        prod "${1:-0}" 3>&-
}

# Whether the instance's own odoo runs, one-off containers left out.
odoo_runs() {
    [ -n "$(docker ps -q --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
        --filter label=com.docker.compose.service=odoo \
        --filter label=com.docker.compose.oneoff=False)" ]
}

# The rows, the services running and what is left in the data directory.
rows() { sql "SELECT string_agg(v, ',' ORDER BY v) FROM t"; }
services() { docker compose ps --services --status running | sort | tr '\n' ' '; }
leftovers() {
    # shellcheck disable=SC2016  # expanded in the container
    docker compose exec -T db sh -c 'ls -a "$PGDATA" | grep -E "^\.(pitr|pre-pitr)" || true'
}

# What every cancel that undid the restore leaves: the data as it was,
# the instance up, nothing of the restore in the data directory.
assert_as_before() {
    wait_db
    [ "$(rows)" = "1-base,2-before-target,3-after-target" ]
    [[ "$(services)" == *"db "*"odoo "* ]]
    [ -z "$(leftovers)" ]
    [ ! -e "$DIR/.walg/restore/lock" ]
    [ ! -e "$DIR/.walg/restore/pid" ]
    [ -z "$(docker ps -aq --filter "name=^pitr-(recovery|fetch)-$JOB\$")" ]
}

@test "a restore left alone takes the database back to the moment" {
    run start_restore
    [ "$status" -eq 0 ]
    run bash "$SCRIPT" restore-wait "$DIR" "$JOB" 240
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    [[ "$(services)" == *"db "*"odoo "* ]]
}

@test "the recovery ignores server settings stored inside the restored copy" {
    sql_admin "ALTER SYSTEM SET recovery_end_command = 'touch /marks/ran-recovery-end'"
    sql_admin "ALTER SYSTEM SET archive_cleanup_command = 'touch /marks/ran-archive-cleanup'"
    sql_admin "SELECT pg_reload_conf()" >/dev/null
    docker compose exec -T db pg_basebackup -U odoo -D - -Ft -X fetch > "$DIR/bases/$BASE.tar"
    sql "INSERT INTO t VALUES ('4-planted-base')"
    sleep 2
    TARGET="$(date -u '+%Y-%m-%d %H:%M:%S')+00"
    sleep 2
    sql "INSERT INTO t VALUES ('5-after-target')"
    sql_admin "SELECT pg_switch_wal()" >/dev/null
    run start_restore
    [ "$status" -eq 0 ]
    run bash "$SCRIPT" restore-wait "$DIR" "$JOB" 240
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    wait_db
    [ "$(rows)" = "1-base,2-before-target,3-after-target,4-planted-base" ]
    [ ! -e "$DIR/marks/ran-recovery-end" ]
    [ ! -e "$DIR/marks/ran-archive-cleanup" ]
}

@test "cancelled while Odoo stops, before anything moved: nothing changed, it runs again" {
    run start_restore
    [ "$status" -eq 0 ]
    wait_log "restoring to" 20
    started=$SECONDS
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE job=$JOB state=failed stopped_at=stopping-odoo"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    [ $((SECONDS - started)) -lt 60 ]
    [ ! -e "$DIR/marks/fetching" ]
    assert_as_before
}

@test "cancelled while the copy is fetched: stopped at once, the data as it was" {
    echo 300 > "$DIR/marks/delay-fetch"
    run start_restore
    [ "$status" -eq 0 ]
    wait_mark fetching 120
    started=$SECONDS
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=failed stopped_at=fetching"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    # Not when the fetch would have ended.
    [ $((SECONDS - started)) -lt 60 ]
    assert_as_before
}

@test "cancelled with the restored copy swapped in and recovering: the previous data is back" {
    echo 4 > "$DIR/marks/delay-wal"
    run start_restore
    [ "$status" -eq 0 ]
    wait_mark wal-fetch 180
    grep -q "restored copy in place" "$DIR/.walg/restore/log"
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=failed stopped_at=recovering"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    grep -q "putting the previous data back" "$DIR/.walg/restore/log"
    assert_as_before
}

@test "cancelled once the restored database came up: the restore finishes and says so" {
    echo 15 > "$DIR/marks/delay-fs"
    run start_restore
    [ "$status" -eq 0 ]
    wait_mark fs-restore 240
    grep -q "promoted on timeline" "$DIR/.walg/restore/log"
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    [[ "$output" == *"filestore=restored"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    [ -e "$DIR/marks/fs-restored" ]
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    [[ "$(services)" == *"db "*"odoo "* ]]
    [ -z "$(leftovers)" ]
}

@test "cancelled while what the database held is archived: stopped at once, nothing changed" {
    # No segment leaves: the drain would wait its 300 s.
    touch "$DIR/marks/hold-archive"
    run start_restore
    [ "$status" -eq 0 ]
    wait_log "restoring to" 20
    # Odoo stopped: the drain runs.
    for _ in $(seq 1 30); do [[ "$(services)" != *"odoo "* ]] && break; sleep 1; done
    sleep 3
    started=$SECONDS
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=failed stopped_at=draining"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    [ $((SECONDS - started)) -lt 60 ]
    [ ! -e "$DIR/marks/fetching" ]
    rm -f "$DIR/marks/hold-archive"
    assert_as_before
}

@test "cancelled with the copy fetched whole and not swapped in yet: nothing of it is left" {
    wrap_docker
    touch "$DIR/marks/hold-after-fetch"
    run start_restore
    [ "$status" -eq 0 ]
    wait_mark fetch-ended 120
    # The window between the fetch and the swap: the copy beside the
    # data, nothing of the data moved.
    top="$(volume_top)"
    echo "$top"
    grep -qx ".pitr-new" <<<"$top"
    [ "$(grep -c "^.pre-pitr-" <<<"$top")" -eq 0 ]
    started=$SECONDS
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 120
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=failed stopped_at=fetching"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    [ $((SECONDS - started)) -lt 60 ]
    rm -f "$DIR/marks/hold-after-fetch"
    assert_as_before
    [ "$(grep -c "restored copy in place" "$DIR/.walg/restore/log")" -eq 0 ]
}

@test "cancelled as the promoted database is put in place: the restore goes on to the end" {
    wrap_docker
    touch "$DIR/marks/hold-promoting"
    run start_restore
    [ "$status" -eq 0 ]
    wait_mark promoting 240
    bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 180 > "$TMP/cancel.out" 2>&1 &
    cancel_pid=$!
    wait_log "cancel asked" 20
    # The TERM reaches the process while it is in that phase.
    sleep 3
    rm -f "$DIR/marks/hold-promoting"
    wait "$cancel_pid"
    cat "$TMP/cancel.out"
    grep -q "IC_PITR_RESTORE job=$JOB state=done" "$TMP/cancel.out"
    grep -q "filestore=restored" "$TMP/cancel.out"
    grep -q "cancelled=1" "$TMP/cancel.out"
    grep -q "goes on to the end" "$DIR/.walg/restore/log"
    [ -e "$DIR/marks/fs-restored" ]
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    [[ "$(services)" == *"db "*"odoo "* ]]
    [ -z "$(leftovers)" ]
    [ ! -e "$DIR/.walg/restore/lock" ]
}

@test "with the update asked for, the restored database is updated after its files, with Odoo stopped" {
    echo 5 > "$DIR/marks/delay-update"
    run start_restore 1
    [ "$status" -eq 0 ]
    wait_mark update 240
    [ -e "$DIR/marks/fs-restored" ]
    # Odoo is not serving the database while it is updated.
    if odoo_runs; then false; fi
    run bash "$SCRIPT" restore-wait "$DIR" "$JOB" 240
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    [[ "$output" == *"modules=updated"* ]]
    [ -e "$DIR/marks/updated" ]
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    # The guard put back the scheduled actions it paused.
    [ "$(sql "SELECT string_agg(cron_name || '=' || active, ',' ORDER BY id) FROM ir_cron")" = "mail queue=true,off=false" ]
    odoo_runs
}

@test "an update that fails leaves the restored database running, and says so" {
    touch "$DIR/marks/fail-update"
    run start_restore 1
    [ "$status" -eq 0 ]
    run bash "$SCRIPT" restore-wait "$DIR" "$JOB" 240
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    [[ "$output" == *"modules=failed"* ]]
    [[ "$output" == *"broken_module failed"* ]]
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    odoo_runs
    [ ! -e "$DIR/.walg/restore/lock" ]
}

@test "cancelled while its modules are updated: the update is not cut, the restore ends" {
    echo 15 > "$DIR/marks/delay-update"
    run start_restore 1
    [ "$status" -eq 0 ]
    wait_mark update 240
    run bash "$SCRIPT" restore-cancel "$DIR" "$JOB" 180
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"state=done"* ]]
    [[ "$output" == *"modules=updated"* ]]
    [[ "$output" == *"cancelled=1"* ]]
    [ -e "$DIR/marks/updated" ]
    grep -q "a cancel came as the restore ends" "$DIR/.walg/restore/log"
    wait_db
    [ "$(rows)" = "1-base,2-before-target" ]
    odoo_runs
}
