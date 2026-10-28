#!/usr/bin/env bash
# Produce a downloadable backup archive for an instance.
#
# Usage:
#   backup_download.sh live-dump    <dir> <dbname> <archive> <db|all> [running]
#   backup_download.sh restore-sql  <dir> <dbname> <time>
#   backup_download.sh package-sql  <dir> <dbname> <tmpdir> <archive>
#   backup_download.sh restore-full <dir> <time>
#   backup_download.sh package-full <dir> <dbname> <tmpdir> <archive>
#   backup_download.sh handover     <dir> <archive> <handoff_dir>
#
# Two families, because the two environments store backups differently:
#
#   * live-dump — non-production has no snapshot history, so the only
#     meaningful "backup" is a dump of the current DB taken on demand.
#     The archive's own directory is bind-mounted so the ZIP lands
#     straight on the host — the job's drop directory, never the whole
#     of /tmp: the container would see every other job's staging there,
#     and leave the dump where any account on the host could read it.
#     ``running`` refuses a stopped database instead of starting it.
#   * restore-* / package-* — production restores the requested
#     timestamp from duplicity inside the ``backup`` container, then
#     copies it out and zips it. Split in two steps so the job log shows
#     the slow restore and the packaging separately.
#
# ``handover`` leaves the archive a job built in its own directory for
# the next job of its chain (a restore): it moves it into <handoff_dir>,
# created here 0700 — a name the panel derives from its secret, so no
# other account on the host knows it in advance. Only a regular file is
# handed over, never a link a container left, and only into a directory
# this call creates. One an earlier attempt of the same job left is this
# account's own and is replaced; anything else at the path refuses. What
# it prints never names <handoff_dir>: the job log is read by more people
# than the host's account. It is in this step's command line while the
# step runs, for ``ps`` to show to whoever is on the host then.
#
# <time> is 'latest' for "most recent" (duplicity's default, no --time
# flag) or a duplicity timestamp already validated panel-side by
# validate_dup_time().
#
# Exit codes:
#   0   done
#   25  ``live-dump ... running`` refused a stopped database: nothing was
#       started nor dumped. Its own code so that a caller can tell it from
#       a dump that failed; the free host backup fails the instance's step
#       on it like on any other, for now, with this script's message in
#       the job log
#   *   anything else failed; the job log says what
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# Working directory *inside* the backup container.
readonly CTR_TMP=/tmp/bkdl
# A live dump with ``running`` refused a stopped database (see above).
readonly EXIT_DB_STOPPED=25

ic_require_args 3 "$#" "backup_download.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

# Build duplicity's --time flag, or nothing at all for 'latest'.
dup_time_flag() {
    [ "$1" = "latest" ] && return 0
    printf -- '--time %s' "$1"
}

case "$op" in
    live-dump)
        ic_require_args 3 "$#" \
            "backup_download.sh live-dump <dir> <dbname> <archive> <db|all> [running]"
        dbname="$1"; archive="$2"; scope="${3:-db}"; only="${4:-}"
        case "$scope" in
            all|db) ;;
            *) ic_die "unknown scope: $scope (expected db or all)" ;;
        esac
        case "$only" in
            ""|running) ;;
            *) ic_die "unknown condition: $only (expected running or nothing)" ;;
        esac
        # A running database is dumped as it runs: ``--no-deps``, or
        # Compose recreates every dependency whose definition changed
        # since the stack started. On 22-oct-2026 a free host's nightly
        # backup did that to a staging whose files template v10 had
        # rewritten: its live ``db`` was recreated under the workers, and
        # a new ``gatekeeper`` started that its running ``odoo`` was not
        # on. A backup never recreates a service.
        #
        # A stopped database is started with what it depends on, as
        # before: a move dumps the source of a panel Sablier put to sleep,
        # and nothing else wakes it. ``running`` (the free host backup,
        # which dumps live only what it saw running) refuses it instead,
        # at once and saying so, with its own exit code.
        running="$(ic_running_services)" \
            || ic_die "could not read which services are running in $dir"
        run_opts=()
        wait_db=()
        if printf '%s\n' "$running" | grep -qx db; then
            # Nor is a gatekeeper started. Template v10's odoo runs in
            # its namespace (``network_mode: service:gatekeeper``), and a
            # stack whose files v10 rewrote but that was not rebuilt has
            # none running (the staging of 22-oct-2026): the one-off
            # would fail in docker's words instead of these.
            services="$(ic_compose_services)" \
                || ic_die "could not read the compose of $dir"
            if printf '%s\n' "$services" | grep -qx gatekeeper \
                && ! printf '%s\n' "$running" | grep -qx gatekeeper; then
                ic_die "the gatekeeper of $dir is not running (not rebuilt under template v10?): a live dump does not start it"
            fi
            # Doodba's own wait for Postgres has no deadline: a database
            # that runs but does not answer the one-off would hold the
            # dump, and the night's backup or the move with it, until the
            # SSH timeout. It is replaced by a bounded one, by the clock
            # and not by attempts (one attempt can take seconds of its
            # own), as in backup_archive.sh; and the unaccent hook, which
            # would query before that wait, is skipped, as in the
            # rebuild's boot test: the live odoo installed the extension
            # when it started.
            run_opts=(--no-deps -e WAIT_DB=false -e UNACCENT=false)
            # shellcheck disable=SC2016  # expanded by the container's bash
            wait_db=(bash -c '
                deadline=$(($(date +%s) + 60))
                until pg_isready -q -t 5; do
                    if [ "$(date +%s)" -ge "$deadline" ]; then
                        echo "[incubacloud] ERROR: the running database did not answer within 60s" >&2
                        exit 1
                    fi
                    sleep 1
                done
                exec "$@"' live-dump)
        elif [ "$only" = running ]; then
            printf '[incubacloud] ERROR: %s\n' \
                "the database of $dir is not running: a live dump does not start it" >&2
            exit "$EXIT_DB_STOPPED"
        else
            ic_log "the database is not running: the dump starts it"
        fi
        ic_log "dumping $dbname live ($scope)"
        ic_db_dump_args "$dbname" "/host-tmp/$(basename "$archive")" "$scope"
        # A one-off, so out of Traefik and Sablier (see ic_compose_run):
        # visitors keep reaching the live odoo while the dump runs.
        ic_compose_run --rm "${run_opts[@]}" \
            -v "$(dirname "$archive"):/host-tmp" odoo \
            "${wait_db[@]}" "${IC_DB_ARGS[@]}"
        ;;

    restore-sql)
        ic_require_args 2 "$#" \
            "backup_download.sh restore-sql <dir> <dbname> <time>"
        dbname="$1"; when="$2"
        ic_log "restoring $dbname.sql from backup ($when)"
        # $DST is the duplicity destination, defined inside the backup
        # container — it must reach it unexpanded, hence the quoting.
        docker compose exec -T backup sh -c \
            "rm -rf $CTR_TMP && mkdir -p $CTR_TMP && dup restore --force \
             $(dup_time_flag "$when") --path-to-restore ${dbname}.sql \
             \"\$DST\" $CTR_TMP/${dbname}.sql"
        ;;

    package-sql)
        ic_require_args 3 "$#" \
            "backup_download.sh package-sql <dir> <dbname> <tmpdir> <archive>"
        dbname="$1"; tmpdir="$2"; archive="$3"
        ic_log "packaging $dbname.sql into $archive"
        mkdir -p "$tmpdir"
        docker compose cp "backup:$CTR_TMP/${dbname}.sql" "$tmpdir/dump.sql"
        docker compose exec -T backup rm -rf "$CTR_TMP"
        (cd "$tmpdir" && zip -r "$archive" dump.sql)
        rm -rf "$tmpdir"
        ;;

    restore-full)
        ic_require_args 1 "$#" \
            "backup_download.sh restore-full <dir> <time>"
        when="$1"
        ic_log "restoring the full backup ($when)"
        docker compose exec -T backup sh -c \
            "rm -rf $CTR_TMP && mkdir -p $CTR_TMP && dup restore --force \
             $(dup_time_flag "$when") \"\$DST\" $CTR_TMP/"
        ;;

    package-full)
        ic_require_args 3 "$#" \
            "backup_download.sh package-full <dir> <dbname> <tmpdir> <archive>"
        dbname="$1"; tmpdir="$2"; archive="$3"
        ic_log "packaging the full backup into $archive"
        mkdir -p "$tmpdir"
        docker compose cp "backup:$CTR_TMP/." "$tmpdir/"
        docker compose exec -T backup rm -rf "$CTR_TMP"
        (
            cd "$tmpdir"
            mv "${dbname}.sql" dump.sql
            # An instance may legitimately have no filestore; the
            # archive then just carries the dump.
            cp -R "odoo/filestore/${dbname}" filestore 2>/dev/null || true
            zip -r "$archive" dump.sql filestore
        )
        rm -rf "$tmpdir"
        ;;

    handover)
        ic_require_args 2 "$#" \
            "backup_download.sh handover <dir> <archive> <handoff_dir>"
        archive="$1"; handoff="$2"
        if [ ! -f "$archive" ] || [ -L "$archive" ]; then
            ic_die "no regular archive to hand over at $archive"
        fi
        if [ -e "$handoff" ] || [ -L "$handoff" ]; then
            if [ -d "$handoff" ] && [ ! -L "$handoff" ] && [ -O "$handoff" ]; then
                ic_log "replacing what an earlier attempt of this job handed over"
                rm -rf "$handoff"
            else
                ic_die "the handoff's directory is already taken: nothing handed over"
            fi
        fi
        mkdir -m 700 "$handoff" 2>/dev/null \
            || ic_die "could not create the handoff's directory"
        mv "$archive" "$handoff/archive.zip"
        ic_log "archive handed over"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
