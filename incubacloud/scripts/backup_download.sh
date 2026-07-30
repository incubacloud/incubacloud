#!/usr/bin/env bash
# Produce a downloadable backup archive for an instance.
#
# Usage:
#   backup_download.sh live-dump    <dir> <dbname> <archive> <db|all>
#   backup_download.sh restore-sql  <dir> <dbname> <time>
#   backup_download.sh package-sql  <dir> <dbname> <tmpdir> <archive>
#   backup_download.sh restore-full <dir> <time>
#   backup_download.sh package-full <dir> <dbname> <tmpdir> <archive>
#
# Two families, because the two environments store backups differently:
#
#   * live-dump — non-production has no snapshot history, so the only
#     meaningful "backup" is a dump of the current DB taken on demand.
#     /tmp is bind-mounted so the ZIP lands straight on the host.
#   * restore-* / package-* — production restores the requested
#     timestamp from duplicity inside the ``backup`` container, then
#     copies it out and zips it. Split in two steps so the job log shows
#     the slow restore and the packaging separately.
#
# <time> is 'latest' for "most recent" (duplicity's default, no --time
# flag) or a duplicity timestamp already validated panel-side by
# validate_dup_time().
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

# Working directory *inside* the backup container.
readonly CTR_TMP=/tmp/bkdl

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
            "backup_download.sh live-dump <dir> <dbname> <archive> <db|all>"
        dbname="$1"; archive="$2"; scope="${3:-db}"
        case "$scope" in
            all) filestore_flag=--filestore ;;
            db) filestore_flag=--no-filestore ;;
            *) ic_die "unknown scope: $scope (expected db or all)" ;;
        esac
        ic_log "dumping $dbname live ($scope)"
        docker compose run --rm -v /tmp:/host-tmp odoo \
            click-odoo-backupdb "$filestore_flag" "$dbname" \
            "/host-tmp/$(basename "$archive")"
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

    *)
        ic_die "unknown operation: $op"
        ;;
esac
