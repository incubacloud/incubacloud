#!/usr/bin/env bash
# Restore a production instance's database from its duplicity backup.
#
# Usage:
#   backup_restore.sh restore    <dir> <time>
#   backup_restore.sh dropdb     <dir> <dbname>
#   backup_restore.sh createdb   <dir> <dbname>
#   backup_restore.sh import-sql <dir> <dbname>
#
# The four container-side steps of the documented doodba restore flow.
# The executor keeps them as separate labelled steps (each with
# stop_on_failure) so a failed restore aborts before dropdb/createdb ever
# run — a failed restore must never fall through to destroying the
# production database and importing a stale/absent dump.
#
# ``$DST``/``$SRC`` are the duplicity destination and source paths
# defined *inside* the backup container, so the commands are single-
# quoted to reach it unexpanded. <time> is a duplicity timestamp already
# validated panel-side by validate_dup_time().
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" "backup_restore.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

case "$op" in
    restore)
        ic_require_args 1 "$#" "backup_restore.sh restore <dir> <time>"
        when="$1"
        ic_log "restoring the backup store into the container ($when)"
        docker compose exec -T backup \
            sh -c "dup restore --time \"$when\" --force \"\$DST\" \"\$SRC\""
        ;;
    dropdb)
        ic_require_args 1 "$#" "backup_restore.sh dropdb <dir> <dbname>"
        dbname="$1"
        ic_log "dropping database $dbname"
        docker compose exec -T backup dropdb --if-exists "$dbname"
        ;;
    createdb)
        ic_require_args 1 "$#" "backup_restore.sh createdb <dir> <dbname>"
        dbname="$1"
        ic_log "creating database $dbname"
        docker compose exec -T backup createdb "$dbname"
        ;;
    import-sql)
        ic_require_args 1 "$#" "backup_restore.sh import-sql <dir> <dbname>"
        dbname="$1"
        ic_log "importing the restored SQL into $dbname"
        docker compose exec -T backup \
            sh -c "psql -v ON_ERROR_STOP=1 -d $dbname -f \$SRC/$dbname.sql"
        ;;
    *)
        ic_die "unknown operation: $op"
        ;;
esac
