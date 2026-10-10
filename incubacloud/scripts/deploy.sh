#!/usr/bin/env bash
# The non-trivial host-side steps of an instance deploy.
#
# Usage:
#   deploy.sh teardown-previous  <dir>
#   deploy.sh copier-deploy      <dir> <answers_file> <template_ref>
#   deploy.sh ensure-secret-key  <dir>
#   deploy.sh inject-secret-env  <dir>
#   deploy.sh cap-hostnames      <dir>
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

    cap-hostnames)
        # Docker refuses a container hostname over 64 bytes (the kernel's
        # __NEW_UTS_LEN), and doodba renders several from the instance's
        # domain: the odoo service's is the domain itself, the backup's
        # "backup.<domain>". An instance under a long domain then failed
        # at its first "docker compose run" ("hostname ... is too long").
        # Every over-long value is cut at a label boundary, keeping its
        # leftmost labels — the ones that tell the service and the
        # instance apart — so the result is still a valid host name. A
        # first label over 63 bytes is itself cut to 63.
        cd "$dir"
        for f in common.yaml prod.yaml test.yaml; do
            [ -f "$f" ] || continue
            awk -v file="$f" '
                function shorten(v,   n, parts, out, i) {
                    n = split(v, parts, ".")
                    out = substr(parts[1], 1, 63)
                    for (i = 2; i <= n; i++) {
                        if (length(out) + 1 + length(parts[i]) > 64) break
                        out = out "." parts[i]
                    }
                    sub(/[.-]+$/, "", out)
                    return out
                }
                /^[ \t]*hostname:[ \t]*/ {
                    match($0, /^[ \t]*hostname:[ \t]*/)
                    head = substr($0, 1, RLENGTH)
                    v = substr($0, RLENGTH + 1)
                    sub(/[ \t]+$/, "", v)
                    q = ""
                    if (v ~ /^".*"$/ || v ~ /^\047.*\047$/) {
                        q = substr(v, 1, 1)
                        v = substr(v, 2, length(v) - 2)
                    }
                    if (length(v) > 64) {
                        s = shorten(v)
                        printf "[incubacloud] capped hostname in %s: %s (%d) -> %s\n", \
                            file, v, length(v), s > "/dev/stderr"
                        print head q s q
                        next
                    }
                }
                { print }
            ' "$f" > "$f.ic-tmp"
            mv "$f.ic-tmp" "$f"
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
