#!/usr/bin/env bash
# Produce a *neutralized* backup archive: a copy of the instance's data
# restored into a throwaway database with ``odoo neutralize`` applied
# (crons off, mail servers archived, credentials scrubbed) so it is safe
# to hand to a developer.
#
# Usage (every operation takes <dir> and <job_id>):
#   backup_neutralized.sh prepare-src-prod  <dir> <job_id> <dbname> <time> <host_tmp>
#   backup_neutralized.sh prepare-src-live  <dir> <job_id> <dbname> <host_tmp>
#   backup_neutralized.sh restore-neutralize <dir> <job_id> <neutral_db>
#   backup_neutralized.sh redump-full       <dir> <job_id> <neutral_db> <archive>
#   backup_neutralized.sh redump-sql        <dir> <job_id> <neutral_db> <host_tmp> <archive>
#   backup_neutralized.sh cleanup           <dir> <job_id> <neutral_db> <host_tmp>
#
# The temporary paths are derived from <job_id> so two concurrent jobs on
# the same host never collide.
#
# Why production repackages its dump as a ZIP: click-odoo-restoredb only
# accepts a ZIP containing ``dump.sql`` or a pg_dump custom-format file.
# A plain .sql takes the pg_restore branch and dies with "Couldn't
# restore database".
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" "backup_neutralized.sh <operation> <dir> <job_id> ..."

op="$1"
dir="$(ic_expand_home "$2")"
job_id="$3"
shift 3

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

# Paths derived from the job id.
ctr_tmp="/tmp/bkneu-${job_id}"                 # in the backup container
src_in_odoo="/tmp/bkneu-src-${job_id}.zip"     # in the odoo container
out_in_odoo="/tmp/bkneu-out-${job_id}"         # in the odoo container

dup_time_flag() {
    [ "$1" = "latest" ] && return 0
    printf -- '--time %s' "$1"
}

case "$op" in
    prepare-src-prod)
        ic_require_args 3 "$#" \
            "prepare-src-prod <dir> <job_id> <dbname> <time> <host_tmp>"
        dbname="$1"; when="$2"; host_tmp="$3"
        ic_log "restoring the source dump from the backup store ($when)"
        # $DST is defined inside the backup container.
        docker compose exec -T backup sh -c \
            "rm -rf $ctr_tmp && mkdir -p $ctr_tmp && dup restore --force \
             $(dup_time_flag "$when") --path-to-restore ${dbname}.sql \
             \"\$DST\" $ctr_tmp/src.sql"
        rm -rf "$host_tmp"
        mkdir -p "$host_tmp"
        docker compose cp "backup:$ctr_tmp/src.sql" "$host_tmp/dump.sql"
        docker compose exec -T backup rm -rf "$ctr_tmp"
        (cd "$host_tmp" && zip -q -r src.zip dump.sql && rm -f dump.sql)
        docker compose cp "$host_tmp/src.zip" "odoo:$src_in_odoo"
        ;;

    prepare-src-live)
        ic_require_args 2 "$#" \
            "prepare-src-live <dir> <job_id> <dbname> <host_tmp>"
        dbname="$1"; host_tmp="$2"
        ic_log "dumping the live database $dbname"
        docker compose exec -T odoo \
            click-odoo-backupdb "$dbname" "$src_in_odoo"
        rm -rf "$host_tmp"
        mkdir -p "$host_tmp"
        ;;

    restore-neutralize)
        ic_require_args 1 "$#" \
            "restore-neutralize <dir> <job_id> <neutral_db>"
        neutral_db="$1"
        ic_log "restoring into $neutral_db with neutralization"
        docker compose exec -T odoo \
            click-odoo-restoredb --neutralize --force \
            "$neutral_db" "$src_in_odoo"
        ;;

    redump-full)
        ic_require_args 2 "$#" \
            "redump-full <dir> <job_id> <neutral_db> <archive>"
        neutral_db="$1"; archive="$2"
        ic_log "dumping $neutral_db with its filestore"
        docker compose exec -T odoo \
            click-odoo-backupdb "$neutral_db" "${out_in_odoo}.zip"
        docker compose cp "odoo:${out_in_odoo}.zip" "$archive"
        docker compose exec -T odoo rm -f "${out_in_odoo}.zip"
        ;;

    redump-sql)
        ic_require_args 3 "$#" \
            "redump-sql <dir> <job_id> <neutral_db> <host_tmp> <archive>"
        neutral_db="$1"; host_tmp="$2"; archive="$3"
        ic_log "dumping $neutral_db (SQL only)"
        # Zipped on the host so the archive layout matches the plain
        # download: a single dump.sql inside.
        docker compose exec -T odoo sh -c \
            "pg_dump --no-owner --no-privileges --dbname=$neutral_db \
             > ${out_in_odoo}.sql"
        docker compose cp "odoo:${out_in_odoo}.sql" "$host_tmp/dump.sql"
        docker compose exec -T odoo rm -f "${out_in_odoo}.sql"
        (cd "$host_tmp" && zip -r "$archive" dump.sql)
        ;;

    cleanup)
        ic_require_args 2 "$#" \
            "cleanup <dir> <job_id> <neutral_db> <host_tmp>"
        neutral_db="$1"; host_tmp="$2"
        ic_log "dropping $neutral_db and removing temporary files"
        # --user root: ``docker compose cp`` lands the source ZIP owned
        # by root, and the default odoo user cannot remove it.
        docker compose exec --user root -T odoo sh -c \
            "dropdb --if-exists $neutral_db \
             && rm -rf /var/lib/odoo/filestore/$neutral_db \
             && rm -f $src_in_odoo"
        rm -rf "$host_tmp"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
