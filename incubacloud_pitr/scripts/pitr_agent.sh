#!/bin/sh
# The continuous recovery agent, run by the instance's ``pitr`` container
# (rclone image): the only place the archive's credentials live.
#
# Usage:
#   pitr_agent.sh loop <interval_minutes> <sync_hour_utc>
#   pitr_agent.sh push                 (upload what is ready now, once)
#   pitr_agent.sh walg <cluster> <wal-g arguments>...
#   pitr_agent.sh base <cluster> full|delta
#   pitr_agent.sh fs-restore <since_day YYYY-MM-DD>
#   pitr_agent.sh fs-purge-deleted <before_day YYYY-MM-DD>
#   pitr_agent.sh purge <cluster>
#
# The container sees the instance's filestore at /src, its database's
# data directory read-only at $PGDATA, wal-g read-only at /opt/walg and
# the spool read-write at /opt/walg/spool.
#
# The database. Its archive_command (pitr_archive.sh) only waits: the
# loop finds the segments PostgreSQL marked ready
# (pg_wal/archive_status/*.ready), uploads each with wal-g and leaves
# spool/done/<segment> once it is in the bucket. Each cluster archives
# under ``$IC_PITR_PREFIX/<system identifier>``, read from pg_control: a
# move or a new deploy starts a new cluster, whose segment names would
# otherwise land on the old one's.
#
# The filestore. Every interval, what changed since the last copy that
# worked (``--max-age``, no listing of the bucket); once a day an exact
# copy, whose deletions are moved, not dropped, to ``deleted/<day>``: an
# attachment removed after the moment a restore goes back to is still
# there to bring back. Odoo names its files by their content, so bringing
# back one too many costs nothing. ``pitrc:`` is an rclone crypt remote
# over the instance's prefix: files are encrypted before they leave.
# spool/fs-ok holds when the last copy that worked started (epoch
# seconds), which the panel's status reading reports.
#
# Installed into the instance's .walg/ by ``pitr.sh install``. IC_SPOOL,
# IC_SRC and IC_WALG override where the spool, the filestore volume and
# wal-g are (tests).
set -eu

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
spool="${IC_SPOOL:-/opt/walg/spool}"
volume="${IC_SRC:-/src}"
src="$volume/filestore"
walg_bin="${IC_WALG:-/opt/walg/run}"
op="${1:-}"

log() {
    printf '[incubacloud] %s\n' "$*"
}

# A YYYY-MM-DD day as a number (20261018), or 0 for anything else.
day_number() {
    case "$1" in
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) echo "$1" | tr -d - ;;
        *) echo 0 ;;
    esac
}

# The database cluster's system identifier: the first 8 bytes of its
# control file, as pg_control_system() reports it.
cluster_id() {
    od -A n -t u8 -N 8 "$PGDATA/global/pg_control" | tr -d ' \n'
}

# wal-g for one cluster.
#   $1     the cluster's system identifier
#   $2...  wal-g arguments
walg() {
    cluster="$1"
    shift
    case "$cluster" in
        '' | *[!0-9]*) log "ERROR: bad cluster: $cluster" >&2; return 2 ;;
    esac
    WALG_S3_PREFIX="$IC_PITR_PREFIX/$cluster" "$walg_bin" "$@"
}

# Upload, in order, the segments PostgreSQL marked ready that are not in
# the bucket yet. The first that fails stops the pass: PostgreSQL asks
# for them in order, so the next ones would only wait for it.
push_ready() {
    [ -f "$PGDATA/global/pg_control" ] || return 0
    cluster="$(cluster_id)" || return 0
    for ready in "$PGDATA"/pg_wal/archive_status/*.ready; do
        [ -e "$ready" ] || continue
        name="$(basename "$ready" .ready)"
        [ -e "$spool/done/$name" ] && continue
        # A copy, so wal-g reads one file and touches nothing in PGDATA.
        work="$(mktemp -d)"
        if cp "$PGDATA/pg_wal/$name" "$work/$name" 2>"$work/err" \
                && walg "$cluster" wal-push "$work/$name" >/dev/null 2>>"$work/err"; then
            touch "$spool/done/$name"
            rm -rf "$work"
            failing=""
            continue
        fi
        now="$(date +%s)"
        if [ "$failing" != "$name" ] || [ $((now - warned)) -ge 300 ]; then
            log "WARNING: could not upload $name: $(tail -n 2 "$work/err" | tr '\n' ' ')"
            failing="$name"
            warned="$now"
        fi
        rm -rf "$work"
        return 0
    done
    # A mark is kept an hour after its segment left the ready list: an
    # archiver that restarted may still ask for it.
    find "$spool/done" -type f -mmin +60 | while read -r mark; do
        [ -e "$PGDATA/pg_wal/archive_status/$(basename "$mark").ready" ] || rm -f "$mark"
    done
}

# Copy the filestore's new files; on success, record when the copy began.
fs_copy() {
    started="$(date +%s)"
    last="$(cat "$spool/fs-ok" 2>/dev/null || echo 0)"
    case "$last" in '' | *[!0-9]*) last=0 ;; esac
    if [ "$last" -eq 0 ]; then
        rclone copy "$src" pitrc:current --fast-list || return 1
    else
        # Since the last copy that worked, however long ago, and a
        # margin for files whose time was set before they landed.
        rclone copy "$src" pitrc:current --no-traverse \
            --max-age "$((started - last + 120))s" || return 1
    fi
    echo "$started" > "$spool/fs-ok"
}

case "$op" in
    loop)
        minutes="$2"
        sync_hour="$3"
        if [ -z "${IC_PITR_PREFIX:-}" ] || [ -z "${RCLONE_CONFIG_PITR_TYPE:-}" ]; then
            # A stack that declares the agent before its credentials
            # exist (the manager's, see its runbook): wait, do not crash.
            log "no recovery credentials in the environment: idle"
            exec sleep infinity
        fi
        mkdir -p "$spool/done" "$src"
        log "uploading the database's log as it is written;" \
            "filestore copy every ${minutes} min, exact copy at ${sync_hour}:00 UTC"
        failing=""
        warned=0
        (
            while true; do
                push_ready || true
                sleep 1
            done
        ) &
        wal_loop=$!
        fs_copy || log "WARNING: first filestore copy failed"
        last_sync=""
        while true; do
            sleep "$((minutes * 60))"
            # The log upload is what the database waits on: without it
            # the container must restart, not carry on half-working.
            kill -0 "$wal_loop" 2>/dev/null || { log "ERROR: the log upload stopped"; exit 1; }
            fs_copy || log "WARNING: filestore copy failed"
            day="$(date -u +%F)"
            hour="$(date -u +%H)"
            if [ "${hour#0}" = "${sync_hour#0}" ] && [ "$last_sync" != "$day" ]; then
                if rclone sync "$src" pitrc:current --fast-list \
                        --backup-dir "pitrc:deleted/$day"; then
                    last_sync="$day"
                    log "exact copy done"
                else
                    log "WARNING: exact copy failed"
                fi
            fi
        done
        ;;
    push)
        mkdir -p "$spool/done"
        failing=""
        warned=0
        push_ready
        ;;
    walg)
        shift
        walg "$@"
        ;;
    base)
        cluster="$2"
        if [ "$3" = "full" ]; then
            walg "$cluster" backup-push --full "$PGDATA"
        else
            walg "$cluster" backup-push "$PGDATA"
        fi
        ;;
    fs-restore)
        since="$2"
        mkdir -p "$src"
        # The volume's own owner is Odoo's user.
        owner="$(stat -c %u:%g "$volume")"
        rclone copy pitrc:current "$src"
        for d in $(rclone lsf --dirs-only pitrc:deleted 2>/dev/null || true); do
            d="${d%/}"
            if [ "$(day_number "$d")" -ge "$(day_number "$since")" ]; then
                rclone copy "pitrc:deleted/$d" "$src"
            fi
        done
        chown -R "$owner" "$src"
        log "filestore restored"
        echo "IC_PITR_FS_RESTORED"
        ;;
    fs-purge-deleted)
        before="$2"
        for d in $(rclone lsf --dirs-only pitrc:deleted 2>/dev/null || true); do
            d="${d%/}"
            n="$(day_number "$d")"
            if [ "$n" -gt 0 ] && [ "$n" -lt "$(day_number "$before")" ]; then
                rclone purge "pitrc:deleted/$d"
                log "dropped deletions of $d"
            fi
        done
        ;;
    purge)
        cluster="$2"
        case "$cluster" in
            '' | *[!0-9]*) log "ERROR: bad cluster: $cluster" >&2; exit 2 ;;
        esac
        # rclone's 3 is "directory not found": nothing left to drop.
        rc=0
        rclone purge "pitr:$cluster" || rc=$?
        [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || exit "$rc"
        log "dropped the archive of cluster $cluster"
        ;;
    *)
        echo "usage: pitr_agent.sh loop|push|walg|base|fs-restore|fs-purge-deleted|purge" >&2
        exit 2
        ;;
esac
