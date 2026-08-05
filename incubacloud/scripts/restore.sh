#!/usr/bin/env bash
# Restore a doodba instance's database from an uploaded .zip backup.
#
# Usage:
#   restore.sh verify-file    <dir> <remote_zip>
#   restore.sh restore-db     <dir> <dbname> <remote_zip> [neutralize]
#   restore.sh set-base-url   <dir> <pg_user> <dbname> <base_url_sql> <report_url>
#   restore.sh ensure-connect <dir> <dbname>
#
# ``restore-db`` takes an optional 4th argument (0/1, default 0): with 1
# it adds --neutralize, which runs every installed module's
# data/neutralize.sql (scheduled actions off, outgoing mail servers
# archived, external providers in test mode, database.is_neutralized=true
# → red banner). Callers that copy a production database into a staging
# instance MUST pass 1; a move between hosts must not, since that is the
# same production instance changing machines.
#
# ``set-base-url`` overwrites the web.base.url/report.url that travelled
# inside the restored dump. It also drops web.base.url.freeze: with the
# freeze set, the copy would stay pinned to the source's domain forever.
#
# ``verify-file`` makes the bind-mounted zip readable from *inside* the
# odoo container: the SSH user and the container's odoo user are
# different UIDs, so a plain ``chmod 600`` leaves click-odoo-restoredb
# with "Path '/mnt/restore.zip' is not readable". The odoo UID is
# discovered dynamically (surviving image upgrades) and the chown/chmod
# use ``sudo`` so it works when the SSH user is a non-root sudoer, which
# every hardened host is.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" "restore.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

case "$op" in
    verify-file)
        ic_require_args 1 "$#" "restore.sh verify-file <dir> <remote_zip>"
        remote="$1"
        [ -f "$remote" ] || ic_die "backup file not found at $remote"
        ic_log "making $remote readable inside the odoo container"
        # ``--entrypoint=''`` skips doodba's addon-linking init so
        # ``id -u`` returns immediately.
        odoo_uid="$(docker compose run --rm --entrypoint="" odoo id -u \
            2>/dev/null | tr -d '\r\n')"
        [ -n "$odoo_uid" ] || ic_die "could not discover the odoo container UID"
        sudo chown "$odoo_uid":"$odoo_uid" "$remote"
        sudo chmod 600 "$remote"
        ;;
    restore-db)
        ic_require_args 2 "$#" \
            "restore.sh restore-db <dir> <dbname> <remote_zip> [neutralize]"
        dbname="$1"; remote="$2"; neutralize="${3:-0}"
        neutralize_flag=()
        if [ "$neutralize" = "1" ]; then
            neutralize_flag=(--neutralize)
            ic_log "restoring $dbname from $remote (neutralized)"
        else
            ic_log "restoring $dbname from $remote"
        fi
        docker compose run --rm -v "$remote:/mnt/restore.zip:ro" \
            odoo click-odoo-restoredb "${neutralize_flag[@]}" --copy --force \
            "$dbname" /mnt/restore.zip
        ;;

    set-base-url)
        ic_require_args 4 "$#" \
            "restore.sh set-base-url <dir> <pg_user> <dbname> <base_url_sql> <report_url>"
        pg_user="$1"; dbname="$2"; base_url_sql="$3"; report_url="$4"
        ic_log "pointing $dbname at $base_url_sql"
        # Guarded by a DO block so it is a no-op if the restore left no
        # ir_config_parameter behind. ``base_url_sql`` arrives already
        # SQL-escaped by the caller (defence in depth on top of the
        # hostname regex constraint on cloud.instance.domain).
        docker compose exec -T db psql -U "$pg_user" -d "$dbname" -c \
"DO \$\$ BEGIN IF EXISTS (SELECT FROM information_schema.tables WHERE \
table_schema='public' AND table_name='ir_config_parameter') THEN \
INSERT INTO ir_config_parameter (key,value) VALUES \
('web.base.url','$base_url_sql'),('report.url','$report_url') \
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value; \
DELETE FROM ir_config_parameter WHERE key='web.base.url.freeze'; \
END IF; END \$\$;"
        ;;
    ensure-connect)
        ic_require_args 1 "$#" "restore.sh ensure-connect <dir> <dbname>"
        dbname="$1"
        ic_log "ensuring incubacloud_connect is installed on $dbname"
        docker compose run --rm odoo \
            odoo -d "$dbname" -i incubacloud_connect --stop-after-init --no-http
        ;;
    *)
        ic_die "unknown operation: $op"
        ;;
esac
