#!/usr/bin/env bash
# Host-side operations of continuous recovery (point-in-time recovery).
#
# Usage:
#   pitr.sh install       <dir> <version> <sha256_amd64> <sha256_aarch64>
#   pitr.sh enable        <dir> <pg_user> <archive_timeout_s>
#   pitr.sh base-backup   <dir> <pg_user> [full]
#   pitr.sh housekeep     <dir> <pg_user> <cutoff_utc> <cutoff_day> [old cluster]...
#   pitr.sh list          <dir> <pg_user>
#   pitr.sh status        <dir> <pg_user>
#   pitr.sh drain         <dir> <pg_user> <timeout_s>
#   pitr.sh restore-start <dir> <pg_user> <job> <cluster> <base> <timeline> <target_utc> <since_day>
#   pitr.sh restore-wait  <dir> <job> <max_s>   (0 also while it still runs)
#   pitr.sh disable       <dir> <pg_user>
#
# Two pieces run in the instance's stack, both from ``<dir>/.walg``:
#
# * ``db`` mounts it read-only at /opt/walg and archives with
#   ``/opt/walg/archive %f`` (pitr_archive.sh), which holds no secret and
#   only waits for the agent: Odoo connects as a superuser, whose SQL can
#   read the database container's environment.
# * the ``pitr`` agent (pitr_agent.sh, rclone image) has the credentials
#   (.docker/pitr.env), uploads the log and the filestore, and runs wal-g
#   for everything else. wal-g's builds are for glibc and the images are
#   Alpine (musl), so ``install`` copies the host's glibc loader and the
#   libraries wal-g needs next to it, and ``run`` starts it through them.
#
# A restore stops the database for as long as it takes, which can outlast
# any one job: ``restore-start`` hands it to a process of its own on the
# host (``restore-run``), which no lost connection or panel restart
# interrupts, and ``restore-wait`` follows it. That process holds a lock
# in .walg/restore/, puts the previous data back if the restored copy does
# not come up, starts the instance again however it ends, and records how
# it ended in .walg/restore/result.
#
# IC_PITR_HOME and IC_PITR_ENV name another directory and environment
# (default .walg and .docker/pitr.env) for a second stack in the same
# directory: the platform's QA copy of its manager.
#
# Nothing secret is ever an argument here.
set -euo pipefail
# Uploaded next to core's scripts, whose lib/ it shares.
# shellcheck source-path=SCRIPTDIR/../../incubacloud/scripts source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "pitr.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2
here="$(cd "$(dirname "$0")" && pwd)"

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

AGENT=pitr
# Where the pieces live, relative to the directory, and the agent's
# environment. Another stack in the same directory (the platform's QA copy
# of its manager) keeps its own: the agent's marks are per stack.
W="${IC_PITR_HOME:-.walg}"
ENVF="${IC_PITR_ENV:-.docker/pitr.env}"
[[ "$W" =~ ^\.[A-Za-z0-9_.-]+$ ]] || ic_die "bad IC_PITR_HOME: $W"
[[ "$ENVF" =~ ^\.docker/[A-Za-z0-9_.-]+\.env$ ]] || ic_die "bad IC_PITR_ENV: $ENVF"
RESTORE="$W/restore"

# ``psql`` in the db container, as the instance's database owner.
#   $1     database user
#   $2...  psql arguments
db_psql() {
    local user="$1"
    shift
    docker compose exec -T db psql -U "$user" -d postgres -v ON_ERROR_STOP=1 -Atq "$@"
}

# The agent, for one operation of pitr_agent.sh.
agent() {
    docker compose exec -T "$AGENT" sh /opt/walg/pitr_agent.sh "$@"
}

# The cluster's system identifier.
#   $1  database user
cluster_id() {
    db_psql "$1" -c "SELECT system_identifier FROM pg_control_system()"
}

# Whether a service of the stack is running now, one-off containers aside.
running() {
    # The whole list is read: grep -q would stop at the first match and,
    # under pipefail, a docker still writing makes it "not running".
    ic_running_services 2>/dev/null | grep -x "$1" >/dev/null
}

# Wait until the database answers.
#   $1  database user
wait_db() {
    for _ in $(seq 1 60); do
        if docker compose exec -T db pg_isready -U "$1" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    ic_die "the database did not come back within 120 s"
}

# Start Odoo again after restart_db stopped it, and say so on a line of
# its own: its metrics series is gone for about a minute, which the
# panel's liveness cron reads as a stop unless the job confirms the
# instance running, as it does on that line.
start_odoo() {
    docker compose start odoo
    echo "IC_PITR_ODOO_RESTARTED"
}

# Restart the database with Odoo stopped around it, so no request meets
# a database going away. Odoo comes back however this ends (start_odoo).
#   $1  database user
restart_db() {
    local was=0
    if running odoo; then
        was=1
        docker compose stop odoo
        trap start_odoo EXIT
    fi
    docker compose restart db
    wait_db "$1"
    if [ "$was" -eq 1 ]; then
        start_odoo
        trap - EXIT
    fi
}

# The newest base copy's name of a cluster, or nothing.
#   $1  cluster
newest_base() {
    agent walg "$1" backup-list --json 2>/dev/null | python3 -c 'import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
print(rows[-1]["backup_name"] if rows else "")' || true
}

# The ``IC_PITR_BASES`` line: a cluster and its base copies.
#   $1  cluster
bases_line() {
    echo "IC_PITR_BASES $1 $(agent walg "$1" backup-list --json --detail | tr -d '\n')"
}

# The last restore's result as ``restore_<key>=<value>`` pairs.
restore_info() {
    [ -f "$RESTORE/result" ] || return 0
    sed 's/^/restore_/' "$RESTORE/result" | tr '\n' ' '
}

case "$op" in
    install)
        ic_require_args 3 "$#" "pitr.sh install <dir> <version> <sha256_amd64> <sha256_aarch64>"
        version="$1"; sha_amd64="$2"; sha_aarch64="$3"
        case "$(uname -m)" in
            x86_64)
                arch=amd64; sha="$sha_amd64"
                loader=/lib64/ld-linux-x86-64.so.2; libdir=/lib/x86_64-linux-gnu ;;
            aarch64)
                arch=aarch64; sha="$sha_aarch64"
                loader=/lib/ld-linux-aarch64.so.1; libdir=/lib/aarch64-linux-gnu ;;
            *) ic_die "unsupported architecture: $(uname -m)" ;;
        esac
        [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || ic_die "bad version: $version"
        [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || ic_die "bad checksum for $arch"
        # One download per host and version; every instance copies it.
        cache="$HOME/.cache/incubacloud/walg/$version"
        bin="$cache/wal-g-pg-20.04-$arch"
        mkdir -p "$cache"
        if ! echo "$sha  $bin" | sha256sum -c --status 2>/dev/null; then
            ic_log "downloading wal-g $version ($arch)"
            # A file of its own: several instances of a host may be
            # switched on at once.
            part="$(mktemp "$cache/.part.XXXXXX")"
            trap 'rm -f "$part"' EXIT
            curl -fsSL --retry 3 -o "$part" \
                "https://github.com/wal-g/wal-g/releases/download/$version/wal-g-pg-20.04-$arch"
            echo "$sha  $part" | sha256sum -c --status \
                || ic_die "wal-g checksum mismatch: refusing it"
            chmod 755 "$part"
            mv -f "$part" "$bin"
            trap - EXIT
        fi
        # The agent's marks start again: a new cluster in this directory
        # may reuse a segment name it left a mark for.
        rm -rf "${W:?}/spool/done"
        mkdir -p "$W/lib" "$W/spool/done"
        chmod 755 "$W" "$W/lib"
        install -m 755 "$bin" "$W/wal-g"
        install -m 755 "$loader" "$W/lib/$(basename "$loader")"
        for lib in libc.so.6 libm.so.6 libpthread.so.0 libresolv.so.2; do
            install -m 755 "$libdir/$lib" "$W/lib/$lib"
        done
        # wal-g starts copies of itself by its argv[0] (the restore's
        # prefetch): --argv0 makes that this wrapper, not the binary,
        # which the container cannot start without the loader.
        cat > "$W/run" <<EOF
#!/bin/sh
# Generated by incubacloud_pitr: run the glibc wal-g build inside a musl
# container through the loader and libc copied next to it.
d=\$(dirname "\$0")
exec "\$d/lib/$(basename "$loader")" --library-path "\$d/lib" --argv0 "\$0" "\$d/wal-g" "\$@"
EOF
        chmod 755 "$W/run"
        install -m 755 "$here/pitr_archive.sh" "$W/archive"
        install -m 755 "$here/pitr_agent.sh" "$W/pitr_agent.sh"
        "$W/run" --version | head -n 1
        ic_log "wal-g $version installed for this instance."
        ;;

    enable)
        ic_require_args 2 "$#" "pitr.sh enable <dir> <pg_user> <archive_timeout_s>"
        user="$1"; timeout="$2"
        [[ "$timeout" =~ ^[0-9]+$ ]] || ic_die "bad archive timeout: $timeout"
        [ -x "$W/archive" ] || ic_die "continuous recovery is not installed here"
        running db || ic_die "the database is not running"
        docker compose exec -T db test -x /opt/walg/archive \
            || ic_die "the archive command is not mounted in the db container: rebuild the instance first"
        # The agent first: once archiving is on, the database waits on it.
        # Recreated, so it starts the script it idled without.
        docker compose up -d --no-deps --force-recreate "$AGENT"
        cluster="$(cluster_id "$user")"
        mode="$(db_psql "$user" -c "SHOW archive_mode")"
        # archive_mode is written down even when the stack's command line
        # has it on: a restore's recovery starts the data directory
        # without that line, and must archive the timeline it begins.
        db_psql "$user" \
            -c "ALTER SYSTEM SET archive_command = '/opt/walg/archive %f'" \
            -c "ALTER SYSTEM SET archive_timeout = '$timeout'" \
            -c "ALTER SYSTEM SET archive_mode = 'on'"
        restarted=0
        if [ "$mode" != "on" ]; then
            # archive_mode only changes with a restart. A stack rendered
            # with this module starts with it on: a reload is enough.
            restart_db "$user"
            restarted=1
        else
            db_psql "$user" -c "SELECT pg_reload_conf()" >/dev/null
        fi
        [ "$(db_psql "$user" -c "SHOW archive_mode")" = "on" ] \
            || ic_die "archive_mode is still off"
        echo "IC_PITR_CLUSTER $cluster restarted=$restarted"
        # One segment end to end, so credentials that do not work fail
        # here and not on the first night.
        before="$(db_psql "$user" -c "SELECT archived_count FROM pg_stat_archiver")"
        db_psql "$user" -c "SELECT txid_current()" -c "SELECT pg_switch_wal()" >/dev/null
        for _ in $(seq 1 90); do
            now="$(db_psql "$user" -c "SELECT archived_count FROM pg_stat_archiver")"
            if [ "$now" -gt "$before" ]; then
                ic_log "archiving works."
                exit 0
            fi
            sleep 1
        done
        docker compose logs --tail 5 "$AGENT" >&2 || true
        ic_die "archiving is on, but no segment reached the bucket within 90 s"
        ;;

    base-backup)
        ic_require_args 1 "$#" "pitr.sh base-backup <dir> <pg_user> [full]"
        user="$1"; kind="${2:-delta}"
        docker compose up -d "$AGENT"
        cluster="$(cluster_id "$user")"
        # A delta only takes the pages changed since its base. A base of
        # another timeline (copied before a restore to the past) holds
        # changes the restored database never had, which a delta on top
        # would carry back: wal-g does not check, so the chain starts
        # again with a full copy whenever the timeline changed, and the
        # first copy of a cluster is a full one.
        timeline="$(db_psql "$user" -c \
            "SELECT lpad(upper(to_hex(timeline_id)), 8, '0') FROM pg_control_checkpoint()")"
        newest="$(newest_base "$cluster")"
        if [ -z "$newest" ] || [ "${newest:5:8}" != "$timeline" ]; then
            kind=full
        fi
        if [ "$kind" = "full" ]; then
            agent base "$cluster" full
        elif ! agent base "$cluster" delta; then
            # A delta builds on the newest copy; after a restore to the
            # past that copy is ahead of the database, and wal-g refuses
            # it. A full copy starts the chain again.
            ic_warn "the delta copy was refused; taking a full one"
            agent base "$cluster" full
        fi
        bases_line "$cluster"
        ;;

    housekeep)
        ic_require_args 3 "$#" "pitr.sh housekeep <dir> <pg_user> <cutoff_utc> <cutoff_day> [old cluster]..."
        user="$1"; cutoff="$2"; cutoff_day="$3"
        shift 3
        [[ "$cutoff" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
            || ic_die "bad cutoff: $cutoff"
        [[ "$cutoff_day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || ic_die "bad day: $cutoff_day"
        docker compose up -d "$AGENT"
        cluster="$(cluster_id "$user")"
        # Each part on its own: one that fails does not keep the others
        # from running, and the step still says it failed.
        failed=0
        # FIND_FULL keeps the full copy a kept delta still needs.
        agent walg "$cluster" delete before FIND_FULL "$cutoff" --confirm \
            || { ic_warn "dropping what left the window failed"; failed=1; }
        agent fs-purge-deleted "$cutoff_day" \
            || { ic_warn "dropping old filestore deletions failed"; failed=1; }
        for old in "$@"; do
            if [ "$old" = "$cluster" ]; then
                ic_warn "refusing to drop the archive of the current cluster $old"
                continue
            fi
            if agent purge "$old"; then
                echo "IC_PITR_PURGED $old"
            else
                ic_warn "dropping the archive of cluster $old failed"
                failed=1
            fi
        done
        bases_line "$cluster"
        exit "$failed"
        ;;

    list)
        ic_require_args 1 "$#" "pitr.sh list <dir> <pg_user>"
        user="$1"
        docker compose up -d "$AGENT"
        bases_line "$(cluster_id "$user")"
        ;;

    status)
        ic_require_args 1 "$#" "pitr.sh status <dir> <pg_user>"
        user="$1"
        if [ -f "$RESTORE/lock" ]; then
            # The database may be down on purpose: nothing to read. A lock
            # whose process is gone (the host rebooted, it was killed) is
            # a restore that never finished, and is said so.
            pid="$(cat "$RESTORE/pid" 2>/dev/null || true)"
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                echo "IC_PITR_STATUS restoring=1 $(restore_info)"
            else
                echo "IC_PITR_STATUS restoring=lost job=$(cat "$RESTORE/lock") $(restore_info)"
            fi
            exit 0
        fi
        row="$(db_psql "$user" -F ' ' -c "SELECT current_setting('archive_mode'),
            coalesce(extract(epoch FROM last_archived_time)::bigint, 0),
            failed_count,
            coalesce(extract(epoch FROM last_failed_time)::bigint, 0),
            (SELECT system_identifier FROM pg_control_system())
            FROM pg_stat_archiver")"
        read -r mode last failed last_failed cluster <<<"$row"
        # Segments waiting for the archive: none means nothing is owed,
        # however long ago the last one left (an idle database).
        pending="$(docker compose exec -T db sh -c '
            now=$(date +%s); n=0; oldest=0
            for f in "$PGDATA"/pg_wal/archive_status/*.ready; do
                [ -e "$f" ] || continue
                n=$((n + 1)); age=$((now - $(stat -c %Y "$f")))
                [ "$age" -gt "$oldest" ] && oldest=$age
            done
            echo "$n $oldest"')"
        read -r n oldest <<<"$pending"
        agent_up=0
        if running "$AGENT"; then agent_up=1; fi
        fs_ok="$(cat "$W/spool/fs-ok" 2>/dev/null || echo 0)"
        [[ "$fs_ok" =~ ^[0-9]+$ ]] || fs_ok=0
        echo "IC_PITR_STATUS mode=$mode last_archived=$last failed=$failed" \
            "last_failed=$last_failed cluster=$cluster pending=$n oldest_pending=$oldest" \
            "agent=$agent_up fs_ok=$fs_ok now=$(date +%s) $(restore_info)"
        ;;

    drain)
        ic_require_args 2 "$#" "pitr.sh drain <dir> <pg_user> <timeout_s>"
        user="$1"; timeout="$2"
        db_psql "$user" -c "SELECT txid_current()" -c "SELECT pg_switch_wal()" >/dev/null
        n=0
        for _ in $(seq 1 "$timeout"); do
            n="$(docker compose exec -T db sh -c \
                'ls "$PGDATA"/pg_wal/archive_status/ | grep -c "\.ready$" || true')"
            if [ "$n" = "0" ]; then
                ic_log "every segment is archived."
                exit 0
            fi
            sleep 1
        done
        ic_die "$n segment(s) still not archived after ${timeout} s"
        ;;

    restore-start)
        ic_require_args 7 "$#" "pitr.sh restore-start <dir> <pg_user> <job> <cluster> <base> <timeline> <target_utc> <since_day>"
        user="$1"; job="$2"; cluster="$3"; backup="$4"; timeline="$5"; target="$6"; since="$7"
        [[ "$job" =~ ^[0-9]+$ ]] || ic_die "bad job: $job"
        [[ "$cluster" =~ ^[0-9]+$ ]] || ic_die "bad cluster: $cluster"
        [[ "$backup" =~ ^base_[0-9A-F_D]+$ ]] || ic_die "bad base backup: $backup"
        [[ "$timeline" =~ ^[0-9]+$ ]] || ic_die "bad timeline: $timeline"
        [[ "$target" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}\+00$ ]] \
            || ic_die "bad target time: $target"
        [[ "$since" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || ic_die "bad day: $since"
        mkdir -p "$RESTORE"
        # The same job again (queue_job runs a job that died once more):
        # follow the restore it started, never start a second one.
        if grep -qx "job=$job" "$RESTORE/result" 2>/dev/null \
                || [ "$(cat "$RESTORE/lock" 2>/dev/null)" = "$job" ]; then
            ic_log "this job's restore is under way or done; following it"
            echo "IC_PITR_RESTORE_STARTED $job"
            exit 0
        fi
        if [ -f "$RESTORE/lock" ]; then
            ic_die "another restore (job $(cat "$RESTORE/lock")) holds this instance: see $dir/$RESTORE/log"
        fi
        [ -x "$W/run" ] || ic_die "continuous recovery is not installed here"
        running "$AGENT" || ic_die "the pitr agent is not running"
        echo "$job" > "$RESTORE/lock"
        rm -f "$RESTORE/result"
        # The process runs from copies: the job's script directory goes
        # when the job ends.
        rm -rf "${RESTORE:?}/bin"
        mkdir -p "$RESTORE/bin/lib"
        install -m 755 "$0" "$RESTORE/bin/pitr.sh"
        install -m 644 "$here/lib/common.sh" "$RESTORE/bin/lib/common.sh"
        : > "$RESTORE/log"
        setsid nohup bash "$RESTORE/bin/pitr.sh" restore-run "$dir" "$user" "$job" \
            "$cluster" "$backup" "$timeline" "$target" "$since" \
            >> "$RESTORE/log" 2>&1 < /dev/null &
        echo "$!" > "$RESTORE/pid"
        ic_log "restore started on the host (job $job); its log is $dir/$RESTORE/log"
        echo "IC_PITR_RESTORE_STARTED $job"
        ;;

    restore-run)
        # Started by restore-start only, detached from the job. The
        # scripts in single quotes run in containers, with their own
        # variables.
        user="$1"; job="$2"; cluster="$3"; backup="$4"; timeline="$5"; target="$6"; since="$7"
        recovery="pitr-recovery-$job"
        stamp="$(date -u +%Y%m%dT%H%M%SZ)"
        state=starting; swapped=0; promoted=0; new_timeline=""; promoted_at=0; fs=no
        say() { printf '[incubacloud] %s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
        # A script on the data directory, in a container of the db
        # service, as root, with $PGDATA set.
        #   $1     the script
        #   $2...  ic_compose_run options (-e, -v)
        in_db() {
            local script="$1"
            shift
            ic_compose_run --rm --no-deps -T -u root "$@" --entrypoint sh db -c "$script"
        }
        # Put the data from before the restore back.
        rollback() {
            say "putting the previous data back"
            docker compose stop db >/dev/null 2>&1 || true
            # shellcheck disable=SC2016  # expanded in the container
            in_db '
                set -eu
                D="$PGDATA"; old="$D/.pre-pitr-$IC_STAMP"; bad="$D/.pitr-failed-$IC_STAMP"
                # Nothing was moved yet: the data is as it was.
                [ -d "$old" ] || exit 0
                mkdir "$bad"
                for f in "$D"/* "$D"/.[!.]*; do
                    [ -e "$f" ] || continue
                    case "$(basename "$f")" in .pre-pitr-*|.pitr-failed-*) continue ;; esac
                    mv "$f" "$bad/"
                done
                for f in "$old"/* "$old"/.[!.]*; do
                    [ -e "$f" ] || continue
                    mv "$f" "$D/"
                done
                rmdir "$old"; rm -rf "$bad"' -e "IC_STAMP=$stamp"
        }
        finish() {
            local rc=$? stopped_at="$state"
            set +e
            docker rm -f "$recovery" >/dev/null 2>&1
            if [ "$state" != "done" ] && [ "$state" != "db-only" ]; then
                say "the restore stopped at '$state' (exit $rc)"
                if [ "$promoted" -eq 1 ]; then
                    # The restored database is in place; what came after
                    # it did not finish.
                    state=db-only
                elif [ "$swapped" -eq 1 ]; then
                    if rollback; then state="failed"; else state="failed-unknown"; fi
                else
                    state=failed
                fi
            fi
            # Whatever happened, the instance runs again.
            docker compose up -d
            {
                echo "job=$job"
                echo "state=$state"
                echo "stopped_at=$stopped_at"
                echo "target=${target/ /T}"
                echo "timeline=$new_timeline"
                echo "promoted_at=$promoted_at"
                echo "filestore=$fs"
            } > "$RESTORE/result"
            rm -f "$RESTORE/lock" "$RESTORE/pid"
            say "restore ended: $state"
        }
        trap finish EXIT
        trap 'exit 129' HUP
        trap 'exit 130' INT
        trap 'exit 143' TERM
        echo "$$" > "$RESTORE/pid"

        say "restoring to $target UTC from $backup (cluster $cluster, timeline $timeline)"
        state=stopping-odoo
        docker compose stop odoo
        state=draining
        # What the database has not handed over yet is archived too, so
        # the present can be gone back to as well. Odoo is stopped first:
        # nothing is written after it.
        bash "$0" drain "$dir" "$user" 300
        state=fetching
        docker compose stop db
        # From here a failure puts back whatever was moved, however far
        # the swap got (nothing, when it had not started).
        swapped=1
        # Fetched beside the current data, then swapped in: wal-g only
        # fetches into an empty directory, and the current data stays
        # whole until the restored copy has come up. Room for a second
        # copy of the data as it is now, and a margin.
        # shellcheck disable=SC2016  # expanded in the container
        in_db '
            set -eu
            D="$PGDATA"; new="$D/.pitr-new"; old="$D/.pre-pitr-$IC_STAMP"
            for f in "$D"/.pre-pitr-*; do
                [ -e "$f" ] || continue
                echo "[incubacloud] ERROR: $f is left from an earlier restore; check it and remove it by hand" >&2
                exit 1
            done
            used=$(du -sk "$D" | cut -f1)
            free=$(df -Pk "$D" | awk "NR==2 {print \$4}")
            needed=$((used * 11 / 10 + 524288))
            if [ "$free" -lt "$needed" ]; then
                echo "[incubacloud] ERROR: ${free} KB free, ${needed} KB needed" >&2
                exit 1
            fi
            while IFS="=" read -r k v; do
                case "$k" in [A-Z]*) export "$k=$v" ;; esac
            done < /run/pitr.env
            export WALG_S3_PREFIX="$IC_PITR_PREFIX/$IC_CLUSTER"
            rm -rf "$new"; mkdir "$new"; chown postgres:postgres "$new"; chmod 700 "$new"
            if ! su postgres -s /bin/sh -c "/opt/walg/run backup-fetch $new $IC_BACKUP"; then
                rm -rf "$new"; exit 1
            fi
            mkdir "$old"
            for f in "$D"/* "$D"/.[!.]*; do
                [ -e "$f" ] || continue
                case "$(basename "$f")" in .pitr-new|.pre-pitr-*) continue ;; esac
                mv "$f" "$old/"
            done
            for f in "$new"/* "$new"/.[!.]*; do
                [ -e "$f" ] || continue
                mv "$f" "$D/"
            done
            rmdir "$new"' \
            -v "$dir/$ENVF:/run/pitr.env:ro" \
            -e "IC_STAMP=$stamp" -e "IC_CLUSTER=$cluster" -e "IC_BACKUP=$backup"
        say "restored copy in place; the previous data is kept until it comes up"
        state=recovering
        # Recovery settings for this start only; reset once promoted.
        # shellcheck disable=SC2016  # expanded in the container
        in_db '
            set -eu
            D="$PGDATA"
            {
                echo "restore_command = '\''/opt/walg/run wal-fetch %f %p'\''"
                echo "recovery_target_time = '\''$IC_TARGET'\''"
                echo "recovery_target_action = '\''promote'\''"
                echo "recovery_target_timeline = '\''$IC_TIMELINE'\''"
            } >> "$D/postgresql.auto.conf"
            touch "$D/recovery.signal"
            chown postgres:postgres "$D/postgresql.auto.conf" "$D/recovery.signal"' \
            -e "IC_TARGET=$target" -e "IC_TIMELINE=$timeline"
        # A database of its own for the recovery, with the credentials
        # wal-fetch needs; the db service never holds them. Odoo is
        # stopped, so nothing of the instance's runs SQL meanwhile.
        docker rm -f "$recovery" >/dev/null 2>&1 || true
        # shellcheck disable=SC2016  # expanded in the container
        ic_compose_run -d --no-deps --name "$recovery" \
            -v "$dir/$ENVF:/run/pitr.env:ro" -e "IC_CLUSTER=$cluster" \
            --entrypoint sh db -c '
            while IFS="=" read -r k v; do
                case "$k" in [A-Z]*) export "$k=$v" ;; esac
            done < /run/pitr.env
            export WALG_S3_PREFIX="$IC_PITR_PREFIX/$IC_CLUSTER"
            exec /autoconf-entrypoint postgres' >/dev/null
        recovery_failed="startup process \(PID [0-9]+\) (exited with exit code [1-9]|was terminated by signal)|terminated by signal|recovery ended before configured recovery target|PANIC:"
        # A long replay is given up to six hours.
        for _ in $(seq 1 10800); do
            st="$(docker exec "$recovery" psql -U "$user" -d postgres -Atqc \
                "SELECT pg_is_in_recovery()" 2>/dev/null || true)"
            if [ "$st" = "f" ]; then
                promoted=1
                break
            fi
            # A recovery that cannot go on stops the server, or says why:
            # no point waiting. Not any FATAL: this very poll gets one
            # while the server is still replaying. The whole log is read
            # (grep -q would stop the pipe early under pipefail).
            if [ "$(docker inspect -f '{{.State.Running}}' "$recovery" 2>/dev/null)" != "true" ] \
                    || docker logs "$recovery" 2>&1 | grep -E "$recovery_failed" >/dev/null; then
                docker logs "$recovery" 2>&1 | grep -E "FATAL|PANIC|terminated" \
                    | grep -v "not yet accepting" | tail -n 5 || true
                break
            fi
            sleep 2
        done
        [ "$promoted" -eq 1 ] || { say "the restored copy did not come up"; exit 1; }
        promoted_at="$(date +%s)"
        new_timeline="$(docker exec "$recovery" psql -U "$user" -d postgres -Atqc \
            "SELECT substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8)" || true)"
        say "promoted on timeline $new_timeline"
        state=promoting
        docker exec "$recovery" psql -U "$user" -d postgres -v ON_ERROR_STOP=1 -Atq \
            -c "ALTER SYSTEM RESET restore_command" \
            -c "ALTER SYSTEM RESET recovery_target_time" \
            -c "ALTER SYSTEM RESET recovery_target_action" \
            -c "ALTER SYSTEM RESET recovery_target_timeline" \
            -c "CHECKPOINT" >/dev/null
        docker stop -t 120 "$recovery" >/dev/null
        docker rm -f "$recovery" >/dev/null 2>&1 || true
        docker compose up -d db
        wait_db "$user"
        # shellcheck disable=SC2016  # expanded in the container
        in_db 'rm -rf "$PGDATA"/.pre-pitr-*'
        state=filestore
        fs=failed
        if ic_compose_run --rm --no-deps -T --entrypoint sh "$AGENT" \
                /opt/walg/pitr_agent.sh fs-restore "$since"; then
            fs=restored
            state="done"
        else
            say "the filestore could not be restored; the database was"
            state=db-only
        fi
        ;;

    restore-wait)
        ic_require_args 2 "$#" "pitr.sh restore-wait <dir> <job> <max_s>"
        job="$1"; max="$2"
        [[ "$job" =~ ^[0-9]+$ ]] || ic_die "bad job: $job"
        [[ "$max" =~ ^[0-9]+$ ]] || ic_die "bad wait: $max"
        shown=0; waited=0
        while :; do
            if [ -f "$RESTORE/log" ]; then
                lines="$(wc -l < "$RESTORE/log")"
                if [ "$lines" -gt "$shown" ]; then
                    sed -n "$((shown + 1)),${lines}p" "$RESTORE/log"
                    shown="$lines"
                fi
            fi
            grep -qx "job=$job" "$RESTORE/result" 2>/dev/null && break
            pid="$(cat "$RESTORE/pid" 2>/dev/null || true)"
            if [ "$(cat "$RESTORE/lock" 2>/dev/null)" != "$job" ] \
                    || ! { [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; }; then
                # Read once more: it may have ended between the two checks.
                grep -qx "job=$job" "$RESTORE/result" 2>/dev/null && break
                echo "IC_PITR_RESTORE state=lost"
                ic_die "no restore of job $job is running here, and none ended: see $dir/$RESTORE/log"
            fi
            if [ "$waited" -ge "$max" ]; then
                # Not a failure: the next job follows it.
                echo "IC_PITR_RESTORE state=running"
                exit 0
            fi
            sleep 5
            waited=$((waited + 5))
        done
        echo "IC_PITR_RESTORE $(tr '\n' ' ' < "$RESTORE/result")"
        case "$(sed -n 's/^state=//p' "$RESTORE/result")" in
            done) exit 0 ;;
            db-only) exit 2 ;;
            *) exit 1 ;;
        esac
        ;;

    disable)
        ic_require_args 1 "$#" "pitr.sh disable <dir> <pg_user>"
        user="$1"
        if [ -f "$RESTORE/lock" ]; then
            ic_die "a restore is running: wait for it"
        fi
        # The settings live in the database. A stopped one is started to
        # drop them, and stopped again: left in place, its next start
        # would archive to a command the rebuild removes, fail for ever
        # and keep every segment.
        started=0
        if ! running db; then
            docker compose up -d db
            wait_db "$user"
            started=1
        fi
        mode="$(db_psql "$user" -c "SHOW archive_mode")"
        # On from the stack's command line, it stays on until the rebuild
        # that follows drops the flag, whatever restarts it: the segments
        # are let go until then instead of kept, and nothing restarts.
        from="$(db_psql "$user" -c \
            "SELECT source FROM pg_settings WHERE name = 'archive_mode'")"
        command="RESET archive_command"
        if [ "$from" = "command line" ]; then
            command="SET archive_command = '/bin/true'"
        fi
        db_psql "$user" \
            -c "ALTER SYSTEM $command" \
            -c "ALTER SYSTEM RESET archive_timeout" \
            -c "ALTER SYSTEM RESET archive_mode"
        if [ "$from" = "command line" ]; then
            db_psql "$user" -c "SELECT pg_reload_conf()" >/dev/null
        elif [ "$mode" = "on" ] && [ "$started" -eq 1 ]; then
            docker compose restart db
            wait_db "$user"
        elif [ "$mode" = "on" ]; then
            restart_db "$user"
        else
            db_psql "$user" -c "SELECT pg_reload_conf()" >/dev/null
        fi
        if [ "$started" -eq 1 ]; then
            docker compose stop db
        fi
        docker compose rm -sf "$AGENT" >/dev/null 2>&1 || true
        # The agent's environment stays (0600, like backup.env): deleting
        # the instance purges the archive with it.
        rm -rf "${W:?}/wal-g" "${W:?}/lib" "${W:?}/run" "${W:?}/archive" \
            "$W/pitr_agent.sh" "$W/spool/done" "$RESTORE"
        echo "IC_PITR_DISABLED"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
