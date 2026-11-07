#!/usr/bin/env bash
# Restore a doodba instance's database from an uploaded .zip backup.
#
# Usage:
#   restore.sh verify-file    <dir> <remote_zip>
#   restore.sh restore-db     <dir> <dbname> <remote_zip> [neutralize] [copy|move]
#   restore.sh set-base-url   <dir> <pg_user> <dbname> <base_url_sql> <report_url>
#   restore.sh use-mail-catcher <dir> <pg_user> <dbname>
#   restore.sh ensure-connect <dir> <pg_user> <dbname> <headless>
#
# ``restore-db`` takes an optional 4th argument (0/1, default 0): with 1
# it adds --neutralize, which runs every installed module's
# data/neutralize.sql (scheduled actions off, outgoing mail servers
# archived, external providers in test mode, database.is_neutralized=true
# → red banner). Callers that copy a production database into a staging
# instance MUST pass 1; a move between hosts must not, since that is the
# same production instance changing machines.
#
# Its optional 5th argument (copy/move, default copy) says whether the
# archive is a copy or the database itself: a move loads it with
# ``move``, so Odoo keeps the source's database.secret (the links it
# signed stay valid), database.uuid and web.base.url, where a copy has
# them regenerated (the base URL to http://localhost:8069). Sessions are
# not carried: they live in the data dir's sessions/, outside the archive.
#
# ``set-base-url`` overwrites the web.base.url/report.url that travelled
# inside the restored dump. It also drops web.base.url.freeze: with the
# freeze set, the copy would stay pinned to the source's domain forever.
#
# ``use-mail-catcher`` removes the placeholder outgoing server that
# Odoo's neutralization inserts (``neutralization - disable emails`` on
# host ``invalid``). Odoo uses any active ``ir.mail_server`` before the
# SMTP in its configuration, so while the placeholder exists every mail
# fails instead of reaching the staging's MailHog (``smtplocal:1025``).
# The real servers stay archived. Only for a staging restored with
# --neutralize: on a production stack ``smtplocal`` is the real relay.
#
# ``verify-file`` makes the bind-mounted zip readable from *inside* the
# odoo container: the SSH user and the container's odoo user are
# different UIDs, so a plain ``chmod 600`` leaves click-odoo-restoredb
# with "Path '/mnt/restore.zip' is not readable". The odoo UID is
# discovered dynamically (surviving image upgrades) and the chown/chmod
# use ``sudo`` so it works when the SSH user is a non-root sudoer, which
# every hardened host is.
#
# ``ensure-connect`` installs incubacloud_connect when the database lacks
# it, with an Odoo that serves nothing. ``headless`` is the option that
# does that on the instance's series (``--no-http`` from 11.0,
# ``--no-xmlrpc`` before), from the series' card in the panel: the
# help of Odoo 19 and later no longer lists the server's options.
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
        # Its path is not printed: a handoff restored in place is named
        # with the panel's secret, and the job log has more readers than
        # the host's account.
        [ -f "$remote" ] || ic_die "backup file not found"
        # ``sudo chown`` and ``chmod`` follow a link: one in the archive's
        # place would hand the file it names to the container's user.
        [ ! -L "$remote" ] || ic_die "the backup file is a link: not used"
        ic_log "making the backup file readable inside the odoo container"
        # ``--entrypoint=''`` skips doodba's addon-linking init so
        # ``id -u`` returns immediately.
        odoo_uid="$(ic_compose_run --rm --entrypoint="" odoo id -u \
            2>/dev/null | tr -d '\r\n')"
        [ -n "$odoo_uid" ] || ic_die "could not discover the odoo container UID"
        sudo chown "$odoo_uid":"$odoo_uid" "$remote"
        sudo chmod 600 "$remote"
        ;;
    restore-db)
        ic_require_args 2 "$#" \
            "restore.sh restore-db <dir> <dbname> <remote_zip> [neutralize] [copy|move]"
        dbname="$1"; remote="$2"; neutralize="${3:-0}"; load_as="${4:-copy}"
        if [ "$neutralize" = "1" ]; then
            ic_log "restoring $dbname (neutralized)"
        elif [ "$load_as" = "move" ]; then
            ic_log "restoring $dbname (moved, not copied)"
        else
            ic_log "restoring $dbname"
        fi
        ic_db_load_args "$dbname" /mnt/restore.zip "$neutralize" "$load_as"
        ic_compose_run --rm -v "$remote:/mnt/restore.zip:ro" \
            odoo "${IC_DB_ARGS[@]}"
        if [ "$neutralize" = "1" ]; then
            ic_db_neutralize_args "$dbname"
            if [ "${#IC_DB_ARGS[@]}" -gt 0 ]; then
                ic_compose_run --rm odoo "${IC_DB_ARGS[@]}"
            fi
        fi
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
    use-mail-catcher)
        ic_require_args 2 "$#" \
            "restore.sh use-mail-catcher <dir> <pg_user> <dbname>"
        pg_user="$1"; dbname="$2"
        ic_log "letting $dbname send through the stack's mail catcher"
        # Guarded like set-base-url; matching on name and host leaves any
        # real server alone, and a database neutralized by an Odoo that
        # inserts no placeholder simply has nothing to delete.
        docker compose exec -T db psql -U "$pg_user" -d "$dbname" -c \
"DO \$\$ BEGIN IF EXISTS (SELECT FROM information_schema.tables WHERE \
table_schema='public' AND table_name='ir_mail_server') THEN \
DELETE FROM ir_mail_server WHERE name='neutralization - disable emails' \
AND smtp_host='invalid'; \
END IF; END \$\$;"
        ;;
    ensure-connect)
        ic_require_args 3 "$#" \
            "restore.sh ensure-connect <dir> <pg_user> <dbname> <headless>"
        pg_user="$1"; dbname="$2"; headless="$3"
        case "$headless" in
            --no-http|--no-xmlrpc) ;;
            *) ic_die "unknown headless option: $headless" ;;
        esac
        # Installed already (a panel restored by a move, most instances):
        # booting an Odoo only to learn that took 7 s of the window a
        # moving customer waits through (QA, 2026-10-20). Any other answer,
        # none included, installs as before.
        state="$(docker compose exec -T db psql -U "$pg_user" -d "$dbname" -tA -c \
"SELECT state FROM ir_module_module WHERE name = 'incubacloud_connect'" \
            2>/dev/null || true)"
        if [ "$state" = "installed" ]; then
            ic_log "incubacloud_connect is already installed on $dbname"
            exit 0
        fi
        ic_log "ensuring incubacloud_connect is installed on $dbname"
        # Headless, so it never takes the port of an Odoo sharing its
        # network.
        ic_compose_run --rm odoo \
            odoo -d "$dbname" -i incubacloud_connect --stop-after-init "$headless"
        ;;
    *)
        ic_die "unknown operation: $op"
        ;;
esac
