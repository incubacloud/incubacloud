#!/usr/bin/env bash
# The non-trivial host-side steps of an instance deploy.
#
# Usage:
#   deploy.sh teardown-previous  <dir>
#   deploy.sh copier-deploy      <dir> <answers_file> <template_ref>
#   deploy.sh ensure-secret-key  <dir>
#   deploy.sh inject-secret-env  <dir>
#   deploy.sh cap-backup-hostname <dir> <project_name>
#   deploy.sh set-system-params  <dir> <pg_user> <dbname> <base_url_sql> <report_url>
#
# The executor keeps each deploy step as its own labelled command (so
# tenant/warm subclasses can still splice by label); this script only
# carries the steps whose bodies were real shell logic. The trivial
# mv/rm/ln steps stay inline in the executor.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "deploy.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

case "$op" in
    teardown-previous)
        # Full teardown of any leftover from a previous failed deploy, so
        # the new one starts from a clean slate with no orphaned
        # containers, networks or stale volumes.
        if [ -d "$dir" ]; then
            ic_log "tearing down the previous deploy at $dir"
            (cd "$dir" && docker compose down --volumes --rmi all \
                --remove-orphans 2>/dev/null) || true
            rm -rf "$dir"
        else
            ic_log "no previous deploy found, clean slate."
        fi
        ;;

    copier-deploy)
        ic_require_args 2 "$#" \
            "deploy.sh copier-deploy <dir> <answers_file> <template_ref> [vcs_ref]"
        answers="$1"; template_ref="$2"; vcs_ref="${3:-}"
        # SSH sessions don't load .bashrc, so ~/.local/bin (pipx tools) is
        # absent from PATH. Export it so copier and every subprocess it
        # spawns (invoke, pre-commit, …) find their tools.
        export PATH="$HOME/.local/bin:$PATH"
        # Read-first guard: full_setup seeds init.defaultBranch once per
        # host, but legacy hosts may still need it. Writing unconditionally
        # takes the ~/.gitconfig lock and races with sibling deploys on the
        # same host (the warm-pool cron enqueues N builds in one tick).
        git config --global --get init.defaultBranch >/dev/null 2>&1 \
            || git config --global init.defaultBranch master
        # Record the template revision every deploy was built from. Without
        # this line an unpinned deploy leaves no trace of which upstream
        # revision produced the tree, which makes "it worked last week"
        # impossible to investigate.
        if [ -n "$vcs_ref" ]; then
            ic_log "running copier into $dir (template $template_ref @ $vcs_ref)"
            "$HOME/.local/bin/copier" copy --defaults --overwrite --trust \
                --vcs-ref "$vcs_ref" \
                --data-file "$answers" "$template_ref" "$dir"
        else
            ic_log "running copier into $dir (template $template_ref @ default branch — unpinned)"
            "$HOME/.local/bin/copier" copy --defaults --overwrite --trust \
                --data-file "$answers" "$template_ref" "$dir"
        fi
        ;;

    ensure-secret-key)
        # Create .docker/incubacloud.env with a fresh Fernet key, unless a
        # previous deploy already left one (leave it — the key must be
        # stable across redeploys or encrypted fields become unreadable).
        env_file="$dir/.docker/incubacloud.env"
        if [ -f "$env_file" ]; then
            ic_log "incubacloud.env already present, keeping its key."
        else
            ic_log "generating a Fernet key in incubacloud.env"
            python3 -c "from cryptography.fernet import Fernet; \
print(f'INCUBACLOUD_SECRET_KEY={Fernet.generate_key().decode()}')" \
                > "$env_file"
        fi
        ;;

    inject-secret-env)
        # Add ``- .docker/incubacloud.env`` to the env_file block of the
        # given compose files (default prod.yaml + test.yaml; the block
        # lives there, not in common.yaml). Idempotent: skipped when
        # already present. Callers that only ship prod.yaml (warm claim)
        # pass it explicitly.
        cd "$dir"
        set -- "$@"
        [ "$#" -gt 0 ] || set -- prod.yaml test.yaml
        for f in "$@"; do
            [ -f "$f" ] || continue
            grep -q 'incubacloud.env' "$f" && continue
            ic_log "injecting incubacloud.env into $f"
            sed -i '/\.docker\/odoo\.env/a\      - .docker/incubacloud.env' "$f"
        done
        ;;

    cap-backup-hostname)
        ic_require_args 1 "$#" \
            "deploy.sh cap-backup-hostname <dir> <project_name>"
        name="$1"
        # doodba renders "hostname: backup.<first_domain>"; a long
        # production domain pushes it past the kernel's 64-byte limit and
        # the backup container dies on start ("sethostname: invalid
        # argument"). Only rewrite when the rendered value exceeds 64,
        # falling back to doodba's own short form ("backup.<project>")
        # capped and stripped of any trailing "." / "-".
        cd "$dir"
        short="backup.$name"
        short="$(printf '%s' "$short" | cut -c1-64 | sed 's/[.-]*$//')"
        for f in common.yaml prod.yaml; do
            [ -f "$f" ] || continue
            cur="$(awk '/hostname: backup/{print $2; exit}' "$f")"
            [ -n "$cur" ] || continue
            if [ "${#cur}" -gt 64 ]; then
                sed -i \
                    "s|hostname:[[:space:]]*backup[^[:space:]]*|hostname: $short|" \
                    "$f"
                ic_log "capped backup hostname: $cur (${#cur}) -> $short"
            else
                ic_log "backup hostname OK: $cur (${#cur})"
            fi
        done
        ;;

    set-system-params)
        ic_require_args 4 "$#" \
            "deploy.sh set-system-params <dir> <pg_user> <dbname> <base_url_sql> <report_url>"
        pg_user="$1"; dbname="$2"; base_url_sql="$3"; report_url="$4"
        # Set web.base.url / report.url before the stack starts so first
        # boot sees the right public URL. Guarded by a DO block so it is a
        # no-op if the DB was not initialised. ``base_url_sql`` arrives
        # already SQL-escaped by the caller (defence in depth on top of the
        # hostname regex constraint).
        cd "$dir"
        docker compose exec -T db psql -U "$pg_user" -d "$dbname" -c \
"DO \$\$ BEGIN IF EXISTS (SELECT FROM information_schema.tables WHERE \
table_schema='public' AND table_name='ir_config_parameter') THEN \
INSERT INTO ir_config_parameter (key,value) VALUES \
('web.base.url','$base_url_sql'),('report.url','$report_url') \
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value; END IF; END \$\$;"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
