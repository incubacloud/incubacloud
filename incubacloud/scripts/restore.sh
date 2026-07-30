#!/usr/bin/env bash
# Restore a doodba instance's database from an uploaded .zip backup.
#
# Usage:
#   restore.sh verify-file    <dir> <remote_zip>
#   restore.sh restore-db     <dir> <dbname> <remote_zip>
#   restore.sh ensure-connect <dir> <dbname>
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
        ic_require_args 2 "$#" "restore.sh restore-db <dir> <dbname> <remote_zip>"
        dbname="$1"; remote="$2"
        ic_log "restoring $dbname from $remote"
        docker compose run --rm -v "$remote:/mnt/restore.zip:ro" \
            odoo click-odoo-restoredb --copy --force "$dbname" /mnt/restore.zip
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
