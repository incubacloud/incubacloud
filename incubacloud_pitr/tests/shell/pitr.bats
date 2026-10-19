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
    *"ps --status running --services"*) printf '%b' "${STUB_RUNNING:-db\nodoo\npitr\n}" ;;
    *"test -x /opt/walg/archive"*) exit "${STUB_ARCHIVE_MOUNTED:-0}" ;;
    *"SHOW archive_mode"*) echo "${STUB_ARCHIVE_MODE:-on}" ;;
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
    rm -rf "$TMP"
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
    grep -q "setsid nohup bash .walg/restore/bin/pitr.sh restore-run $DIR odoo 9 " "$CALLS"
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
    export STUB_RUNNING='odoo\n' STUB_ARCHIVE_MODE=on
    run bash "$SCRIPT" disable "$DIR" odoo
    [ "$status" -eq 0 ]
    up="$(grep -n "compose up -d db" "$CALLS" | cut -d: -f1)"
    reset="$(grep -n "RESET archive_command" "$CALLS" | cut -d: -f1)"
    stop="$(grep -n "compose stop db" "$CALLS" | cut -d: -f1)"
    [ "$up" -lt "$reset" ] && [ "$reset" -lt "$stop" ]
    # Not Odoo: it was not the one stopped.
    ! grep -q "compose stop odoo" "$CALLS"
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
