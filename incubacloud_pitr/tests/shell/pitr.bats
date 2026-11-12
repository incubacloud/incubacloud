#!/usr/bin/env bats
# Tests for scripts/pitr.sh, pitr_agent.sh and pitr_archive.sh.
#
# docker, curl, rclone and wal-g are stubbed: what is checked here is the
# scripts' own logic (refusals, the wal-g pin, the timeline rule, which
# cluster is purged, how a restore is started, followed and switched off
# around, the date arithmetic, the order of uploads). The scripts were
# also run end to end against Alpine PostgreSQL and an S3 server
# (18/19-oct-2026): archiving through the agent, base copies, restores to
# a minute — one past an earlier restore —, the rollback of one that
# cannot finish, an interrupted one, and switching off a stopped database.

setup() {
    SRC="${BATS_TEST_DIRNAME}/../../scripts"
    TMP="$(mktemp -d)"
    # pitr.sh sources lib/common.sh next to it, as on the host where it
    # is uploaded beside core's scripts.
    mkdir -p "$TMP/scripts/lib"
    cp "$SRC"/*.sh "$TMP/scripts/"
    cp "${BATS_TEST_DIRNAME}/../../../incubacloud/scripts/lib/common.sh" "$TMP/scripts/lib/"
    cp "${BATS_TEST_DIRNAME}/../../../incubacloud/scripts/cron_guard.sh" "$TMP/scripts/"
    SCRIPT="$TMP/scripts/pitr.sh"
    AGENT="$TMP/scripts/pitr_agent.sh"
    export HOME="$TMP/home"
    DIR="$HOME/projects/inst"
    mkdir -p "$DIR/.walg" "$DIR/.docker" "$TMP/bin"
    export CALLS="$TMP/calls"
    : > "$CALLS"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"compose ps -q --status running"*) printf '%b' "${STUB_RUNNING:-db\nodoo\npitr\n}" ;;
    # ic_running_services: the ids above are the service names.
    *"com.docker.compose.oneoff"*) shift 3; printf '%s\n' "$@" ;;
    *"test -x /opt/walg/archive"*) exit "${STUB_ARCHIVE_MOUNTED:-0}" ;;
    *"pg_isready"*) exit "${STUB_PG_READY:-0}" ;;
    *"SHOW archive_mode"*) echo "${STUB_ARCHIVE_MODE:-on}" ;;
    *"FROM pg_settings WHERE name = 'archive_mode'"*) echo "${STUB_ARCHIVE_SOURCE:-configuration file}" ;;
    *"current_setting('archive_command')"*) printf '%s\n' "${STUB_ARCHIVE_COMMAND-}" ;;
    *"RESET archive_timeout"*) exit "${STUB_RESET_STATUS:-0}" ;;
    # One more segment archived on every read: the end-to-end check passes.
    *"archived_count"*) n="$(cat "$CALLS.archived" 2>/dev/null || echo 0)"; echo $((n + 1)) > "$CALLS.archived"; echo "$n" ;;
    *"pg_control_checkpoint"*) echo "${STUB_TIMELINE:-00000002}" ;;
    *"pg_control_system"*) echo 7698138691441184802 ;;
    *"backup-list --json"*) echo "${STUB_BASES:-[]}" ;;
    *"pitr_agent.sh purge "*) exit "${STUB_PURGE_STATUS:-0}" ;;
    *"pitr_agent.sh fs-purge-deleted"*) exit "${STUB_FS_PURGE_STATUS:-0}" ;;
esac
exit 0
STUB
    cat > "$TMP/bin/setsid" <<'STUB'
#!/usr/bin/env bash
echo "setsid $*" >> "$CALLS"
STUB
    chmod +x "$TMP/bin/docker" "$TMP/bin/setsid"
    export PATH="$TMP/bin:$PATH"
}

teardown() {
    # A restore process a test started, and whatever it left running.
    pkill -KILL -f "$TMP/" 2>/dev/null || true
    rm -rf "$TMP"
}

# Wait for a line of $CALLS that matches the grep pattern $1: restore-start
# launches setsid in the background, and the stub writes its line a moment
# after the command returns. Polls up to 50 times, 0.1 s apart; returns 0 at
# the first match and 1 when none shows up.
wait_call() {
    local i
    for i in $(seq 1 50); do
        grep -q "$1" "$CALLS" && return 0
        sleep 0.1
    done
    return 1
}

# A process standing for a restore's: it notes each TERM in $TMP/term and
# goes on, as restore-run does once its database is promoted. Its pid is
# in .walg/restore/pid, the lock names job $1.
fake_restore() {
    mkdir -p "$DIR/.walg/restore"
    echo "$1" > "$DIR/.walg/restore/lock"
    bash -c "trap 'echo TERM >> \"$TMP/term\"' TERM; while :; do sleep 0.2; done" \
        restore-run "$DIR" user "$1" &
    echo "$!" > "$DIR/.walg/restore/pid"
    touch "$DIR/.walg/restore/armed"
}

# ── pitr.sh ──────────────────────────────────────────────────────────────

@test "install refuses a version that is not one" {
    run bash "$SCRIPT" install "$DIR" 'v3;rm' aa bb
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad version"* ]]
}

@test "install refuses a download whose checksum is not the pinned one, and leaves no part" {
    cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && { echo tampered > "$2"; }; shift; done
STUB
    chmod +x "$TMP/bin/curl"
    sha=0000000000000000000000000000000000000000000000000000000000000000
    run bash "$SCRIPT" install "$DIR" v3.0.9 "$sha" "$sha"
    [ "$status" -ne 0 ]
    [[ "$output" == *"checksum mismatch"* ]]
    [ ! -e "$DIR/.walg/wal-g" ]
    # Each install downloads to a file of its own, removed on failure.
    [ -z "$(find "$HOME/.cache/incubacloud/walg" -name '.part.*')" ]
}

@test "install downloads wal-g retrying every kind of error with a growing wait, with time limits" {
    cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS"
while [ "$#" -gt 0 ]; do [ "$1" = "-o" ] && { echo tampered > "$2"; }; shift; done
STUB
    chmod +x "$TMP/bin/curl"
    sha=0000000000000000000000000000000000000000000000000000000000000000
    run bash "$SCRIPT" install "$DIR" v3.0.9 "$sha" "$sha"
    line="$(grep '^curl ' "$CALLS")"
    # Up to 15 minutes of backoff (1 s doubling, curl's own): a GitHub
    # outage outlasted a fixed 10 s wait six times over (QA, 8-nov-2026).
    [[ "$line" == *"--retry 10 "* ]]
    [[ "$line" == *"--retry-max-time 900"* ]]
    [[ "$line" != *"--retry-delay"* ]]
    [[ "$line" == *" -fsSL "* ]]
    [[ "$line" == *"--retry-all-errors"* ]]
    [[ "$line" == *"--connect-timeout 20"* ]]
    [[ "$line" == *"--max-time 300"* ]]
}

@test "enable refuses a timeout that is not a number" {
    run bash "$SCRIPT" enable "$DIR" odoo '60; reboot'
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad archive timeout"* ]]
}

@test "enable stops when the archive command is not mounted yet" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MOUNTED=1
    run bash "$SCRIPT" enable "$DIR" odoo 300
    [ "$status" -ne 0 ]
    [[ "$output" == *"rebuild the instance first"* ]]
    ! grep -q "ALTER SYSTEM" "$CALLS"
}

@test "enable starts the agent before the database waits on it" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" enable "$DIR" odoo 300
    agent="$(grep -n "force-recreate pitr" "$CALLS" | cut -d: -f1)"
    command="$(grep -n "archive_command" "$CALLS" | head -n 1 | cut -d: -f1)"
    [ -n "$agent" ] && [ -n "$command" ]
    [ "$agent" -lt "$command" ]
    grep -q "ALTER SYSTEM SET archive_command = '/opt/walg/archive %f'" "$CALLS"
}

@test "enable on a database already archiving reloads it and stops nothing" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" enable "$DIR" odoo 300
    [ "$status" -eq 0 ]
    [[ "$output" == *"restarted=0"* ]]
    grep -q "pg_reload_conf" "$CALLS"
    # Written down too, for a restore's recovery, started without the
    # stack's command line.
    grep -q "ALTER SYSTEM SET archive_mode = 'on'" "$CALLS"
    ! grep -q "compose stop odoo" "$CALLS"
    ! grep -q "compose restart db" "$CALLS"
}

@test "enable restarts a database not archiving yet, with Odoo stopped around it" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=off
    run bash "$SCRIPT" enable "$DIR" odoo 300
    stop="$(grep -n "compose stop odoo" "$CALLS" | cut -d: -f1)"
    restart="$(grep -n "compose restart db" "$CALLS" | cut -d: -f1)"
    start="$(grep -n "compose start odoo" "$CALLS" | cut -d: -f1)"
    [ -n "$stop" ] && [ "$stop" -lt "$restart" ] && [ "$restart" -lt "$start" ]
}

@test "a restart that stopped Odoo says it started it again" {
    # The job confirms the instance running on that line: the metrics
    # cron would read the gap in Odoo's series as a stop.
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=off
    # The stub still answers "off" after the restart, so the switch stops
    # there: past what is checked here.
    run bash "$SCRIPT" enable "$DIR" odoo 300
    [[ "$output" == *"archive_mode is still off"* ]]
    # On a line of its own: the executor reads it whole.
    grep -qx "IC_PITR_ODOO_RESTARTED" <<< "$output"
}

@test "a restart with Odoo already stopped leaves it so, and says nothing of it" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=off STUB_RUNNING='db\npitr\n'
    run bash "$SCRIPT" enable "$DIR" odoo 300
    grep -q "compose restart db" "$CALLS"
    [[ "$(cat "$CALLS")" != *"compose start odoo"* ]]
    [[ "$output" != *"IC_PITR_ODOO_RESTARTED"* ]]
}

@test "Odoo is started again, and says so, when the database does not come back" {
    install -m 755 /dev/null "$DIR/.walg/archive"
    export STUB_ARCHIVE_MODE=off STUB_PG_READY=1
    # The two minutes wait_db gives the database, at once.
    printf '#!/usr/bin/env bash\n' > "$TMP/bin/sleep"
    chmod +x "$TMP/bin/sleep"
    run bash "$SCRIPT" enable "$DIR" odoo 300
    [ "$status" -ne 0 ]
    [[ "$output" == *"did not come back"* ]]
    grep -q "compose start odoo" "$CALLS"
    [[ "$output" == *"IC_PITR_ODOO_RESTARTED"* ]]
}

@test "a base copy after a timeline change is a full one" {
    export STUB_TIMELINE=00000002
    export STUB_BASES='[{"backup_name":"base_00000001000000000000000A"}]'
    run bash "$SCRIPT" base-backup "$DIR" odoo
    [ "$status" -eq 0 ]
    grep -q "pitr_agent.sh base 7698138691441184802 full" "$CALLS"
}

@test "a base copy on the same timeline is a delta" {
    export STUB_TIMELINE=00000002
    export STUB_BASES='[{"backup_name":"base_00000002000000000000000D"}]'
    run bash "$SCRIPT" base-backup "$DIR" odoo
    [ "$status" -eq 0 ]
    grep -q "pitr_agent.sh base 7698138691441184802 delta" "$CALLS"
    ! grep -q "base 7698138691441184802 full" "$CALLS"
}

@test "the first base copy of a cluster is a full one" {
    export STUB_BASES='[]'
    run bash "$SCRIPT" base-backup "$DIR" odoo
    [ "$status" -eq 0 ]
    grep -q "pitr_agent.sh base 7698138691441184802 full" "$CALLS"
}

@test "housekeeping refuses a cutoff that is not a UTC timestamp" {
    run bash "$SCRIPT" housekeep "$DIR" odoo 'yesterday' 2026-10-01
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad cutoff"* ]]
}

@test "housekeeping never drops the cluster archiving now" {
    run bash "$SCRIPT" housekeep "$DIR" odoo 2026-10-01T00:00:00Z 2026-10-01 \
        7698138691441184802 1111
    [ "$status" -eq 0 ]
    ! grep -q "pitr_agent.sh purge 7698138691441184802" "$CALLS"
    grep -q "pitr_agent.sh purge 1111" "$CALLS"
    [[ "$output" == *"IC_PITR_PURGED 1111"* ]]
    [[ "$output" == *"IC_PITR_BASES 7698138691441184802"* ]]
}

@test "one housekeeping part that fails does not stop the others, and is said" {
    export STUB_FS_PURGE_STATUS=1 STUB_PURGE_STATUS=1
    run bash "$SCRIPT" housekeep "$DIR" odoo 2026-10-01T00:00:00Z 2026-10-01 1111
    [ "$status" -eq 1 ]
    grep -q "pitr_agent.sh purge 1111" "$CALLS"
    [[ "$output" != *"IC_PITR_PURGED"* ]]
    [[ "$output" == *"IC_PITR_BASES"* ]]
}

@test "a restore refuses a target or a base copy it cannot vouch for" {
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_0000000100 1 '2026-10-18; date' 2026-10-18
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad target time"* ]]
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 'base_1;id' 1 '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad base backup"* ]]
    ! grep -q "setsid" "$CALLS"
}

@test "a restore is handed to a process of its own, holding the instance" {
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE_STARTED 9"* ]]
    [ "$(cat "$DIR/.walg/restore/lock")" = "9" ]
    wait_call "setsid nohup bash .walg/restore/bin/pitr.sh restore-run $DIR odoo 9 "
    [ -f "$DIR/.walg/restore/bin/lib/common.sh" ]
}

@test "the same job again follows its restore instead of starting another" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -eq 0 ]
    ! grep -q "setsid" "$CALLS"
    rm "$DIR/.walg/restore/lock"
    printf 'job=9\nstate=done\n' > "$DIR/.walg/restore/result"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -eq 0 ]
    ! grep -q "setsid" "$CALLS"
}

@test "another job's restore holds the instance" {
    mkdir -p "$DIR/.walg/restore"
    echo 8 > "$DIR/.walg/restore/lock"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -ne 0 ]
    [[ "$output" == *"another restore (job 8)"* ]]
}

@test "following a restore reports how it ended" {
    mkdir -p "$DIR/.walg/restore"
    printf 'job=9\nstate=db-only\nfilestore=failed\n' > "$DIR/.walg/restore/result"
    run bash "$SCRIPT" restore-wait "$DIR" 9 60
    [ "$status" -eq 2 ]
    [[ "$output" == *"IC_PITR_RESTORE job=9 state=db-only filestore=failed"* ]]
}

@test "a restore still running when the wait ends is not a failure" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    sleep 30 &
    echo "$!" > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" restore-wait "$DIR" 9 0
    kill %1 || true
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=running"* ]]
}

@test "a restore whose process died without a word is lost" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    echo 999999 > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" restore-wait "$DIR" 9 60
    [ "$status" -ne 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=lost"* ]]
}

@test "the status says a restore runs instead of reading a stopped database" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    sleep 30 &
    echo "$!" > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" status "$DIR" odoo
    kill %1 || true
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_STATUS restoring=1"* ]]
    ! grep -q "pg_stat_archiver" "$CALLS"
}

@test "a lock whose process is gone is a restore that never finished" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    echo 999999 > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" status "$DIR" odoo
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_STATUS restoring=lost job=9"* ]]
}

@test "a cancel with no restore of its job says so, and the restore never starts after it" {
    run bash "$SCRIPT" restore-cancel "$DIR" 9 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=none"* ]]
    # The start that comes after the cancel (it was on its way) refuses.
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -ne 0 ]
    [[ "$output" == *"cancelled before it started"* ]]
    [ ! -e "$DIR/.walg/restore/lock" ]
    [ "$(grep -c setsid "$CALLS")" -eq 0 ]
}

@test "a cancel of another job does not keep a restore from starting" {
    run bash "$SCRIPT" restore-cancel "$DIR" 8 0
    [ "$status" -eq 0 ]
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -eq 0 ]
    wait_call "setsid"
}

@test "a cancel of a restore that ended reports how it ended" {
    mkdir -p "$DIR/.walg/restore"
    printf 'job=9\nstate=failed\nstopped_at=recovering\ncancelled=1\n' > "$DIR/.walg/restore/result"
    run bash "$SCRIPT" restore-cancel "$DIR" 9 60
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE job=9 state=failed stopped_at=recovering cancelled=1"* ]]
}

@test "a cancel stops the restore's process and waits for it to say how it ended" {
    fake_restore 9
    run bash "$SCRIPT" restore-cancel "$DIR" 9 0
    [ "$status" -eq 0 ]
    # Still running when the wait ended: not a failure, and the next job follows it.
    [[ "$output" == *"IC_PITR_RESTORE state=running"* ]]
    for _ in $(seq 1 10); do [ -s "$TMP/term" ] && break; sleep 0.2; done
    [ "$(cat "$TMP/term")" = "TERM" ]
    [ "$(cat "$DIR/.walg/restore/cancel")" = "9" ]
    grep -q "cancel asked (job 9)" "$DIR/.walg/restore/log"
}

@test "a restore still running while its modules are updated says so, to a wait and to a cancel" {
    fake_restore 9
    run bash "$SCRIPT" restore-wait "$DIR" 9 0
    [[ "$output" == *"IC_PITR_RESTORE state=running"* ]]
    [[ "$output" != *"phase="* ]]
    echo "[incubacloud] 10:00:00 updating the restored database's modules to the code the instance runs (for up to 3600 s)" \
        >> "$DIR/.walg/restore/log"
    run bash "$SCRIPT" restore-wait "$DIR" 9 0
    [[ "$output" == *"IC_PITR_RESTORE state=running phase=update"* ]]
    run bash "$SCRIPT" restore-cancel "$DIR" 9 0
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=running phase=update"* ]]
}

@test "a cancel whose restore's process died without a word says it is lost" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    echo 999999 > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" restore-cancel "$DIR" 9 60
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=lost"* ]]
}

@test "a cancel does not signal a pid that is not the restore of its job" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    touch "$DIR/.walg/restore/armed"
    # A process of someone else, on a pid the dead restore left behind.
    bash -c 'trap "echo TERM >> \"$1/term\"" TERM; while :; do sleep 0.2; done' x "$TMP" &
    other=$!
    echo "$other" > "$DIR/.walg/restore/pid"
    run bash "$SCRIPT" restore-cancel "$DIR" 9 0
    sleep 1
    alive=0; kill -0 "$other" 2>/dev/null && alive=1
    kill -KILL "$other" 2>/dev/null || true
    [ "$status" -eq 0 ]
    [ "$alive" -eq 1 ]
    [ ! -e "$TMP/term" ]
    [[ "$output" == *"is not the restore of job 9"* ]]
}

@test "a cancel while another job's restore holds the instance says whose it is, and stops nothing" {
    fake_restore 8
    run bash "$SCRIPT" restore-cancel "$DIR" 9 60
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE state=none held_by=8"* ]]
    [[ "$output" != *"state=lost"* ]]
    [ ! -e "$TMP/term" ]
}

@test "a restore cancelled while it drains stops what the drain started before the stack comes up" {
    # The drain's reads of pg_wal never end: it would wait its 300 s.
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"grep -c"*)
        echo "exec-start" >> "$CALLS"
        trap 'echo exec-killed >> "$CALLS"; exit 143' TERM
        sleep 3 & wait "$!"
        echo "exec-end" >> "$CALLS"
        echo 1 ;;
esac
exit 0
STUB
    mkdir -p "$DIR/.walg/restore"
    bash "$SCRIPT" restore-run "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 > "$TMP/run.log" 2>&1 3>&- &
    run_pid=$!
    for _ in $(seq 1 50); do grep -q exec-start "$CALLS" && break; sleep 0.2; done
    grep -q exec-start "$CALLS"
    kill -TERM "$run_pid"
    for _ in $(seq 1 40); do kill -0 "$run_pid" 2>/dev/null || break; sleep 0.5; done
    cat "$TMP/run.log"
    if kill -0 "$run_pid" 2>/dev/null; then false; fi
    grep -qx "state=failed" "$DIR/.walg/restore/result"
    grep -qx "stopped_at=draining" "$DIR/.walg/restore/result"
    # Every read the drain started ended before the stack was brought up,
    # and none after it.
    up="$(grep -n "docker compose up -d$" "$CALLS" | head -n 1 | cut -d: -f1)"
    [ -n "$up" ]
    before="$(head -n "$up" "$CALLS")"
    after="$(tail -n "+$up" "$CALLS")"
    [ "$(grep -c exec-start <<<"$before")" -eq "$(grep -cE "exec-(killed|end)" <<<"$before")" ]
    [ "$(grep -cE "exec-(start|killed|end)" <<<"$after")" -eq 0 ]
}

@test "the recovery's server is started with the panel's settings" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"grep -c"*) echo 0 ;;
    *"pg_is_in_recovery"*) echo f ;;
    *"pg_walfile_name"*) echo 00000002 ;;
    *"PG_VERSION"*) echo 17 ;;
esac
exit 0
STUB
    mkdir -p "$DIR/.walg/restore"
    run bash "$SCRIPT" restore-run "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 3>&-
    echo "$output"
    # The server's own command line.
    start="$(awk '/--name pitr-recovery-9 /,/exec \/autoconf-entrypoint/' "$CALLS")"
    [ -n "$start" ]
    for setting in "restore_command=/opt/walg/run wal-fetch %f %p" \
            "archive_command=/opt/walg/archive %f" recovery_end_command= \
            archive_cleanup_command= ssl_passphrase_command= \
            shared_preload_libraries= session_preload_libraries= \
            local_preload_libraries= jit=off; do
        grep -qF -- "-c \"$setting\"" <<<"$start" || { echo "missing: $setting"; false; }
    done
    grep -qF 'archive_library=' <<<"$start"
    grep -qF 'event_triggers=off' <<<"$start"
    # Each connection to it sets the same.
    execs="$(grep "^docker exec .*pitr-recovery-9 psql" "$CALLS")"
    [ -n "$execs" ]
    if grep -v -- "-e PGOPTIONS=-c search_path=pg_catalog .*-c event_triggers=off" <<<"$execs"; then false; fi
}

# A docker for a restore that runs to its end: the recovery promotes at
# once, the files come back. click-odoo-update exits $STUB_UPDATE_STATUS,
# the files' restore $STUB_FS_STATUS.
promoting_docker() {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"grep -c"*) echo 0 ;;
    *"pg_is_in_recovery"*) echo f ;;
    *"pg_walfile_name"*) echo 00000002 ;;
    *"PG_VERSION"*) echo 17 ;;
    *"inspect -f {{.State.Running}}"*) echo true ;;
    *"click-odoo-update"*) exit "${STUB_UPDATE_STATUS:-0}" ;;
    *"fs-restore"*) exit "${STUB_FS_STATUS:-0}" ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
}

# Run a restore to its end, its update asked for ($1, default 1).
run_restore() {
    mkdir -p "$DIR/.walg/restore"
    run bash "$SCRIPT" restore-run "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 prod "${1:-1}" 3>&-
    echo "$output"
}

# The line of $CALLS the first call matching $1 is on.
call_line() { grep -n -- "$1" "$CALLS" | head -n 1 | cut -d: -f1; }

@test "a restore with the update asked for hands the database and the flag to its process, with the guard beside it" {
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 prod 1
    [ "$status" -eq 0 ]
    wait_call "setsid nohup bash .walg/restore/bin/pitr.sh restore-run $DIR odoo 9 .* 2026-10-18 prod 1$"
    [ -x "$DIR/.walg/restore/bin/cron_guard.sh" ]
}

@test "a restore refuses an update flag or a database it cannot vouch for" {
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 prod 'yes;id'
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad update flag"* ]]
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 'prod;id' 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad database"* ]]
    ! grep -q "setsid" "$CALLS"
    [ ! -e "$DIR/.walg/restore/lock" ]
}

@test "a restore started with eight arguments needs no guard beside it, and copies none" {
    # As a caller that stages pitr.sh and lib/ only (an older panel, or a
    # stack whose task stages them by hand) starts it: no update asked.
    rm -f "$TMP/scripts/cron_guard.sh"
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18
    [ "$status" -eq 0 ]
    [[ "$output" == *"IC_PITR_RESTORE_STARTED 9"* ]]
    wait_call "setsid nohup bash .walg/restore/bin/pitr.sh restore-run $DIR odoo 9 .* 2026-10-18  0$"
    [ ! -e "$DIR/.walg/restore/bin/cron_guard.sh" ]
}

@test "a restore asked to update refuses to start without the guard beside it" {
    rm -f "$TMP/scripts/cron_guard.sh"
    install -m 755 /dev/null "$DIR/.walg/run"
    run bash "$SCRIPT" restore-start "$DIR" odoo 9 7698 base_000000010000000000000005 1 \
        '2026-10-18 22:30:48+00' 2026-10-18 prod 1
    [ "$status" -ne 0 ]
    [[ "$output" == *"cron_guard.sh is missing next to pitr.sh"* ]]
    ! grep -q "setsid" "$CALLS"
    [ ! -e "$DIR/.walg/restore/lock" ]
}

@test "the restored database's modules are updated once its files are back, before the stack comes up" {
    promoting_docker
    run_restore 1
    [ "$status" -eq 0 ]
    fs="$(call_line "fs-restore")"
    update="$(call_line "click-odoo-update --database prod")"
    up="$(call_line "docker compose up -d$")"
    [ -n "$fs" ] && [ -n "$update" ] && [ -n "$up" ]
    [ "$fs" -lt "$update" ] && [ "$update" -lt "$up" ]
    # Odoo was not started before the update.
    ! head -n "$update" "$CALLS" | grep -qE "compose (start|up -d$|up -d odoo)"
    grep -qx "state=done" "$DIR/.walg/restore/result"
    grep -qx "modules=updated" "$DIR/.walg/restore/result"
}

@test "an update that fails leaves the restore done and the instance running, and says so" {
    promoting_docker
    STUB_UPDATE_STATUS=1 run_restore 1
    [ "$status" -eq 0 ]
    grep -qx "state=done" "$DIR/.walg/restore/result"
    grep -qx "modules=failed" "$DIR/.walg/restore/result"
    update="$(call_line "click-odoo-update")"
    up="$(call_line "docker compose up -d$")"
    [ -n "$up" ] && [ "$update" -lt "$up" ]
    [[ "$output" == *"updating the modules failed (exit 1)"* ]]
}

@test "an update that outlasts its limit has its Odoo container removed, and the instance starts" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"grep -c"*) echo 0 ;;
    *"pg_is_in_recovery"*) echo f ;;
    *"pg_walfile_name"*) echo 00000002 ;;
    *"PG_VERSION"*) echo 17 ;;
    *"inspect -f {{.State.Running}}"*) echo true ;;
    # An Odoo that ignores the TERM, as PID 1 does: it ends once removed.
    *"click-odoo-update"*)
        while [ ! -e "$CALLS.removed" ]; do sleep 0.1; done
        exit 137 ;;
    *"compose ps -a -q odoo"*) printf 'own\noneoff\n' ;;
    *"com.docker.compose.oneoff"*) if [ "${*: -1}" = oneoff ]; then echo True; else echo False; fi ;;
    "rm -f oneoff") touch "$CALLS.removed" ;;
esac
exit 0
STUB
    chmod +x "$TMP/bin/docker"
    IC_PITR_UPDATE_TIMEOUT_S=2 run_restore 1
    [ "$status" -eq 0 ]
    grep -qx "state=done" "$DIR/.walg/restore/result"
    grep -qx "modules=failed" "$DIR/.walg/restore/result"
    # The one-off only: the instance's own odoo is started again.
    removed="$(call_line "docker rm -f oneoff")"
    up="$(call_line "docker compose up -d$")"
    [ -n "$removed" ] && [ -n "$up" ] && [ "$removed" -lt "$up" ]
    ! grep -q "docker rm -f own" "$CALLS"
    [[ "$output" == *"the update ran for 2 s: its Odoo container is removed"* ]]
    [[ "$output" == *"partly updated"* ]]
}

@test "the update's time limit is a number" {
    IC_PITR_UPDATE_TIMEOUT_S='1;id' run bash "$SCRIPT" status "$DIR" odoo
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad IC_PITR_UPDATE_TIMEOUT_S"* ]]
}

@test "a database restored without its files is updated all the same" {
    promoting_docker
    STUB_FS_STATUS=1 run_restore 1
    grep -qx "state=db-only" "$DIR/.walg/restore/result"
    grep -qx "modules=updated" "$DIR/.walg/restore/result"
}

@test "without the update asked for nothing is updated" {
    promoting_docker
    run_restore 0
    [ "$status" -eq 0 ]
    ! grep -q "click-odoo-update" "$CALLS"
    grep -qx "modules=skipped" "$DIR/.walg/restore/result"
}

@test "a restore that never replaced the database updates nothing" {
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"grep -c"*) echo 0 ;;
    *"PG_VERSION"*) echo 17 ;;
    # The recovery stops at once: nothing was promoted.
    *"inspect -f {{.State.Running}}"*) echo false ;;
esac
exit 0
STUB
    run_restore 1
    grep -qx "state=failed" "$DIR/.walg/restore/result"
    grep -qx "modules=skipped" "$DIR/.walg/restore/result"
    ! grep -q "click-odoo-update" "$CALLS"
}

@test "installing again forgets the marks of a cluster that may be gone" {
    mkdir -p "$DIR/.walg/spool/done"
    touch "$DIR/.walg/spool/done/000000010000000000000001"
    bin="$HOME/.cache/incubacloud/walg/v3.0.9/wal-g-pg-20.04-amd64"
    mkdir -p "$(dirname "$bin")"
    echo binary > "$bin"
    sha="$(sha256sum "$bin" | cut -d' ' -f1)"
    run bash "$SCRIPT" install "$DIR" v3.0.9 "$sha" "$sha"
    # The run of the binary itself fails here (not one); the marks went first.
    [ ! -e "$DIR/.walg/spool/done/000000010000000000000001" ]
    [ -d "$DIR/.walg/spool/done" ]
}

@test "switching off a stopped database starts it, drops the settings, stops it again" {
    export STUB_RUNNING='\n' STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -eq 0 ]
    up="$(grep -n "compose up -d db" "$CALLS" | cut -d: -f1)"
    reset="$(grep -n "RESET archive_mode" "$CALLS" | cut -d: -f1)"
    stop="$(grep -n "compose stop db" "$CALLS" | cut -d: -f1)"
    [ "$up" -lt "$reset" ] && [ "$reset" -lt "$stop" ]
    # Not Odoo: it was not the one stopped.
    ! grep -q "compose stop odoo" "$CALLS"
}

@test "switching off a database restarting under a running Odoo leaves it up" {
    # Odoo up and the database not: a restart in progress, not a stopped
    # instance. Stopping it would take the instance down.
    export STUB_RUNNING='odoo\n' STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -eq 0 ]
    ! grep -q "compose stop db" "$CALLS"
    # Its restart goes around Odoo, as for any running instance.
    stop="$(grep -n "compose stop odoo" "$CALLS" | cut -d: -f1)"
    restart="$(grep -n "compose restart db" "$CALLS" | cut -d: -f1)"
    start="$(grep -n "compose start odoo" "$CALLS" | cut -d: -f1)"
    [ -n "$stop" ] && [ "$stop" -lt "$restart" ] && [ "$restart" -lt "$start" ]
}

@test "switching off a database the stack keeps archiving lets its segments go, without a restart" {
    export STUB_ARCHIVE_MODE=on STUB_ARCHIVE_SOURCE='command line'
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -eq 0 ]
    grep -q "ALTER SYSTEM SET archive_command = '/bin/true'" "$CALLS"
    ! grep -q "RESET archive_command" "$CALLS"
    grep -q "pg_reload_conf" "$CALLS"
    ! grep -q "compose stop odoo" "$CALLS"
    ! grep -q "compose restart db" "$CALLS"
}

@test "switching off a database archiving from its settings restarts it with Odoo around it, and says so" {
    export STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -eq 0 ]
    stop="$(grep -n "compose stop odoo" "$CALLS" | cut -d: -f1)"
    restart="$(grep -n "compose restart db" "$CALLS" | cut -d: -f1)"
    start="$(grep -n "compose start odoo" "$CALLS" | cut -d: -f1)"
    [ -n "$stop" ] && [ "$stop" -lt "$restart" ] && [ "$restart" -lt "$start" ]
    [[ "$output" == *"IC_PITR_ODOO_RESTARTED"* ]]
}

@test "switching off lets the segments go before anything else, wherever archive_mode comes from" {
    # A command that needs no mount: a cut anywhere after it, or a rebuild
    # without the mount, leaves no database keeping its segments.
    for source in 'configuration file' 'command line'; do
        : > "$CALLS"
        export STUB_ARCHIVE_MODE=on STUB_ARCHIVE_SOURCE="$source"
        run bash "$SCRIPT" disable "$DIR" odoo
        [ "$status" -eq 0 ]
        first="$(grep -n "ALTER SYSTEM" "$CALLS" | head -n 1)"
        [[ "$first" == *"ALTER SYSTEM SET archive_command = '/bin/true'"* ]]
        [[ "$first" == *"pg_reload_conf"* ]]
        ! grep -q "RESET archive_command" "$CALLS"
    done
}

@test "a switch off cut after its first write leaves the segments let go" {
    export STUB_ARCHIVE_MODE=on STUB_RESET_STATUS=1
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -ne 0 ]
    command="$(grep -n "SET archive_command = '/bin/true'" "$CALLS" | cut -d: -f1)"
    reset="$(grep -n "RESET archive_timeout" "$CALLS" | cut -d: -f1)"
    [ -n "$command" ] && [ "$command" -lt "$reset" ]
    grep "SET archive_command = '/bin/true'" "$CALLS" | grep -q "pg_reload_conf"
}

@test "settling a database left with archiving on and no command lets its segments go" {
    export STUB_ARCHIVE_COMMAND='' STUB_ARCHIVE_SOURCE='command line'
    run bash "$SCRIPT" settle "$DIR" odoo -
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    grep -q "ALTER SYSTEM SET archive_command = '/bin/true'" "$CALLS"
    grep -q "ALTER SYSTEM RESET archive_timeout" "$CALLS"
    grep -q "ALTER SYSTEM RESET archive_mode" "$CALLS"
    grep -q "pg_reload_conf" "$CALLS"
    # Never a restart: no database stops under the customer for this.
    ! grep -q "compose restart db" "$CALLS"
    ! grep -q "compose stop" "$CALLS"
}

@test "settling a known cluster with no command still parks it" {
    # Switched off by a plan change: the panel still knows its base
    # copies, but nothing archives it, and the segments would pile up.
    export STUB_ARCHIVE_COMMAND=''
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    grep -q "ALTER SYSTEM SET archive_command = '/bin/true'" "$CALLS"
}

@test "settling a known cluster that archives with its command keeps it" {
    export STUB_ARCHIVE_COMMAND='/opt/walg/archive %f'
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED kept" <<< "$output"
    ! grep -q "ALTER SYSTEM" "$CALLS"
    ! grep -q "pg_reload_conf" "$CALLS"
}

@test "settling another cluster that archives parks it" {
    # A move or a new deploy: the panel's base copies are of another
    # cluster, and this one's log restores nothing without a base.
    export STUB_ARCHIVE_COMMAND='/opt/walg/archive %f'
    run bash "$SCRIPT" settle "$DIR" odoo 1234
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    grep -q "ALTER SYSTEM SET archive_command = '/bin/true'" "$CALLS"
    run bash "$SCRIPT" settle "$DIR" odoo -
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
}

@test "settling any other command parks it" {
    export STUB_ARCHIVE_COMMAND='cp %p /mnt/old/%f'
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    grep -q "ALTER SYSTEM SET archive_command = '/bin/true'" "$CALLS"
}

@test "settling a database already parked writes nothing" {
    export STUB_ARCHIVE_COMMAND='/bin/true'
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    ! grep -q "ALTER SYSTEM" "$CALLS"
    ! grep -q "pg_reload_conf" "$CALLS"
}

@test "settling leaves the agent as it was" {
    # An orphaned switch on may be about to write the real command: an
    # agent sent back to idle then would keep every segment.
    echo agent > "$DIR/.walg/pitr_agent.sh"
    for command in '' '/opt/walg/archive %f' '/bin/true'; do
        export STUB_ARCHIVE_COMMAND="$command"
        run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
        [ "$status" -eq 0 ]
    done
    [ -f "$DIR/.walg/pitr_agent.sh" ]
    ! grep -qE "compose (up|rm|stop|restart|kill).* pitr" "$CALLS"
    ! grep -q "force-recreate" "$CALLS"
}

@test "settling a stopped database starts it, writes, and stops it again" {
    export STUB_RUNNING='\n' STUB_ARCHIVE_COMMAND=''
    run bash "$SCRIPT" settle "$DIR" odoo -
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED parked" <<< "$output"
    up="$(grep -n "compose up -d db" "$CALLS" | cut -d: -f1)"
    command="$(grep -n "SET archive_command = '/bin/true'" "$CALLS" | cut -d: -f1)"
    stop="$(grep -n "compose stop db" "$CALLS" | cut -d: -f1)"
    [ -n "$up" ] && [ "$up" -lt "$command" ] && [ "$command" -lt "$stop" ]
    ! grep -q "compose stop odoo" "$CALLS"
}

@test "settling a stopped database it keeps stops it again too" {
    export STUB_RUNNING='\n' STUB_ARCHIVE_COMMAND='/opt/walg/archive %f'
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    grep -qx "IC_PITR_SETTLED kept" <<< "$output"
    grep -q "compose stop db" "$CALLS"
}

@test "settling a database restarting under a running Odoo leaves it up" {
    # A TERM during the switch's restart, or an abort meeting one: Odoo
    # is up and the database is coming back. Stopping it would take the
    # instance down.
    for command in '' '/opt/walg/archive %f' '/bin/true'; do
        : > "$CALLS"
        export STUB_RUNNING='odoo\n' STUB_ARCHIVE_COMMAND="$command"
        run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
        [ "$status" -eq 0 ]
        grep -q "IC_PITR_SETTLED" <<< "$output"
        ! grep -q "compose stop" "$CALLS"
    done
}

@test "settling during a restore touches nothing" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    export STUB_ARCHIVE_COMMAND=''
    run bash "$SCRIPT" settle "$DIR" odoo -
    [ "$status" -eq 0 ]
    grep -qx "IC_PITR_SETTLED restoring" <<< "$output"
    [ ! -s "$CALLS" ]
}

@test "settling twice gives the same answer" {
    export STUB_ARCHIVE_COMMAND='/opt/walg/archive %f'
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    first="$output"
    run bash "$SCRIPT" settle "$DIR" odoo 7698138691441184802
    grep -qx "IC_PITR_SETTLED kept" <<< "$first"
    [ "$(grep IC_PITR_SETTLED <<< "$first")" = "$(grep IC_PITR_SETTLED <<< "$output")" ]
}

@test "settling refuses a cluster that is not one" {
    run bash "$SCRIPT" settle "$DIR" odoo '1;reboot'
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad cluster"* ]]
    [ ! -s "$CALLS" ]
}

@test "switching off waits for a restore" {
    mkdir -p "$DIR/.walg/restore"
    echo 9 > "$DIR/.walg/restore/lock"
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -ne 0 ]
    ! grep -q "RESET" "$CALLS"
}

# ── pitr_archive.sh and pitr_agent.sh ─────────────────────────────────────

agent_env() {
    export IC_SPOOL="$TMP/spool" IC_SRC="$TMP/src" IC_WALG="$TMP/bin/walg"
    export PGDATA="$TMP/pgdata" IC_PITR_PREFIX=s3://bucket/p/inst-pitr
    mkdir -p "$IC_SPOOL/done" "$IC_SRC/filestore" "$PGDATA/global" "$PGDATA/pg_wal/archive_status"
    # A control file whose first 8 bytes are the system identifier 42.
    printf '\052\000\000\000\000\000\000\000' > "$PGDATA/global/pg_control"
    cat > "$TMP/bin/walg" <<'STUB'
#!/bin/sh
echo "walg $WALG_S3_PREFIX $*" >> "$CALLS"
case "$*" in *"$STUB_WALG_FAIL"*) [ -n "$STUB_WALG_FAIL" ] && exit 1 ;; esac
exit 0
STUB
    cat > "$TMP/bin/rclone" <<'STUB'
#!/usr/bin/env bash
echo "rclone $*" >> "$CALLS"
[ "$1" = lsf ] && printf '2026-10-01/\n2026-10-10/\n2026-10-11/\nnot-a-day/\n'
[ "$1" = purge ] && exit "${STUB_RCLONE_PURGE:-0}"
exit 0
STUB
    chmod +x "$TMP/bin/walg" "$TMP/bin/rclone"
}

@test "the archive command succeeds once the agent marked the segment" {
    agent_env
    touch "$IC_SPOOL/done/000000010000000000000003"
    run sh "$TMP/scripts/pitr_archive.sh" 000000010000000000000003
    [ "$status" -eq 0 ]
    export IC_ARCHIVE_WAIT=1
    run sh "$TMP/scripts/pitr_archive.sh" 000000010000000000000004
    [ "$status" -ne 0 ]
    [[ "$output" == *"not uploaded"* ]]
}

@test "the agent uploads ready segments in order, under the cluster, and marks them" {
    agent_env
    for s in 000000010000000000000001 000000010000000000000002; do
        echo "$s" > "$PGDATA/pg_wal/$s"
        touch "$PGDATA/pg_wal/archive_status/$s.ready"
    done
    run sh "$AGENT" push
    [ "$status" -eq 0 ]
    [ -e "$IC_SPOOL/done/000000010000000000000001" ]
    [ -e "$IC_SPOOL/done/000000010000000000000002" ]
    grep -q "walg s3://bucket/p/inst-pitr/42 wal-push .*/000000010000000000000001" "$CALLS"
    first="$(grep -n "0000000000000001$" "$CALLS" | cut -d: -f1)"
    second="$(grep -n "0000000000000002$" "$CALLS" | cut -d: -f1)"
    [ "$first" -lt "$second" ]
}

@test "the agent stops at the first segment that fails, and marks nothing after" {
    agent_env
    for s in 000000010000000000000001 000000010000000000000002; do
        echo "$s" > "$PGDATA/pg_wal/$s"
        touch "$PGDATA/pg_wal/archive_status/$s.ready"
    done
    export STUB_WALG_FAIL=000000010000000000000001
    run sh "$AGENT" push
    [ "$status" -eq 0 ]
    [[ "$output" == *"could not upload 000000010000000000000001"* ]]
    [ ! -e "$IC_SPOOL/done/000000010000000000000001" ]
    ! grep -q "0000000000000002$" "$CALLS"
}

@test "only the days before the window are dropped from the deletions" {
    agent_env
    run sh "$AGENT" fs-purge-deleted 2026-10-11
    [ "$status" -eq 0 ]
    grep -q "purge pitrc:deleted/2026-10-01" "$CALLS"
    grep -q "purge pitrc:deleted/2026-10-10" "$CALLS"
    ! grep -q "purge pitrc:deleted/2026-10-11" "$CALLS"
    ! grep -q "not-a-day" <(grep purge "$CALLS")
}

@test "a filestore restore brings back what was deleted since its day" {
    agent_env
    run sh "$AGENT" fs-restore 2026-10-10
    [ "$status" -eq 0 ]
    grep -q "copy pitrc:current $IC_SRC/filestore" "$CALLS"
    grep -q "copy pitrc:deleted/2026-10-10 " "$CALLS"
    grep -q "copy pitrc:deleted/2026-10-11 " "$CALLS"
    ! grep -q "copy pitrc:deleted/2026-10-01 " "$CALLS"
    [[ "$output" == *"IC_PITR_FS_RESTORED"* ]]
}

@test "purging a cluster: only a number, and an empty prefix is no failure" {
    agent_env
    run sh "$AGENT" purge '../x'
    [ "$status" -ne 0 ]
    ! grep -q "rclone purge" "$CALLS"
    export STUB_RCLONE_PURGE=3
    run sh "$AGENT" purge 1111
    [ "$status" -eq 0 ]
    grep -q "rclone purge pitr:1111" "$CALLS"
}

@test "another stack in the same directory keeps pieces of its own" {
    # The manager's QA copy shares the production directory on its host.
    mkdir -p "$DIR/.walg-qa/restore" "$DIR/.walg/restore"
    echo 7 > "$DIR/.walg/restore/lock"
    export IC_PITR_HOME=.walg-qa IC_PITR_ENV=.docker/qa-pitr.env
    run bash "$SCRIPT" status "$DIR" odoo
    [ "$status" -eq 0 ]
    [[ "$output" != *"restoring"* ]]
    grep -q "pg_stat_archiver" "$CALLS"
}

@test "a home or an environment outside the directory is refused" {
    IC_PITR_HOME=../elsewhere run bash "$SCRIPT" status "$DIR" odoo
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad IC_PITR_HOME"* ]]
    IC_PITR_ENV=/etc/shadow run bash "$SCRIPT" status "$DIR" odoo
    [ "$status" -ne 0 ]
    [[ "$output" == *"bad IC_PITR_ENV"* ]]
}
