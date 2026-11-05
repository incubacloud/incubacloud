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
#   deploy.sh record-heads       <dir> <spec_file>
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

# Seconds one ``git ls-remote`` may take in record-heads. GitHub answers
# in well under a second; a host that cannot reach it should not hold
# the build for minutes (5-nov-2026).
HEADS_TIMEOUT=20

# Print the commit(s) <ref> names at <url>, space-separated, or nothing.
#
# Usage: remote_ref_head <git_home> <url> <ref>
#   <git_home>  an empty owner-only directory, git's HOME for the call
#   <url>       the repository, as repos.yaml names it (token included)
#   <ref>       a branch or tag name, or a full ``refs/...`` name
#
# A plain name is asked exactly as ``refs/heads/<ref>`` and
# ``refs/tags/<ref>``: a bare pattern also matches ``refs/heads/x/main``
# or ``refs/pull/*/head``. Both are kept when both exist, since a
# fetch prefers the tag. An https URL's credentials travel as an
# ``Authorization`` header in that HOME's config, not in git's command
# line, which every user of the host can read. git runs without the
# host's configuration (see record-heads), says nothing, and asks
# nothing. Credentials go only to https://github.com itself: a URL
# that carries them to any other host, port or scheme is not asked at
# all, nor one with a ? # or \ before its path (5-nov-2026). Returns
# git's status, 124 (137 if KILL ended it) when it ran out of time,
# 1 for such a URL.
remote_ref_head() {
    local home="$1" url="$2" ref="$3" auth="" authority out
    local wanted=("refs/heads/$ref" "refs/tags/$ref")
    [[ "$ref" != refs/* ]] || wanted=("$ref")
    authority="${url#*://}"
    authority="${authority%%/*}"
    # A ? # or \ before the first / may end the host before the last @,
    # as in ``TOK@evil.example?@github.com/x``, whose host is evil.example
    # for curl: parsers do not agree on it, so credentials with one of
    # them there are not sent at all (5-nov-2026).
    if [[ "$authority" == *@* ]] \
        && [[ "$url" != https://* || "${authority##*@}" != github.com \
            || "$authority" == *[?#\\]* ]]; then
        return 1
    fi
    if [[ "$url" =~ ^(https://)([^/@]+)@(.+)$ ]] \
        && [[ "${BASH_REMATCH[2]}" != *%* ]]; then
        auth="$(printf '%s' "${BASH_REMATCH[2]}" | base64 -w0)"
        url="${BASH_REMATCH[1]}${BASH_REMATCH[3]}"
    fi
    (
        umask 077
        if [ -n "$auth" ]; then
            printf '[http]\n\textraHeader = Authorization: Basic %s\n' "$auth"
        fi > "$home/.gitconfig"
    )
    out="$(
        cd "$home" && env -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_SYSTEM \
            -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT -u GIT_DIR \
            -u GIT_ASKPASS -u SSH_ASKPASS \
            HOME="$home" XDG_CONFIG_HOME="$home/.config" \
            GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
            GIT_CEILING_DIRECTORIES="$(dirname "$home")" \
            timeout -k 5 "$HEADS_TIMEOUT" \
            git -c credential.helper= -c protocol.version=2 \
                ls-remote -- "$url" "${wanted[@]}" 2>/dev/null </dev/null
    )" || return
    awk -v a="${wanted[0]}" -v b="${wanted[1]:-}" \
        '$2 == a || ($2 == b && b != "") {print $1}' <<< "$out" \
        | sort -u | paste -sd ' '
}

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
            ic_copier copy --defaults --overwrite --trust \
                --vcs-ref "$vcs_ref" \
                --data-file "$answers" "$template_ref" "$dir"
        else
            ic_log "running copier into $dir (template $template_ref @ default branch — unpinned)"
            ic_copier copy --defaults --overwrite --trust \
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

    record-heads)
        ic_require_args 1 "$#" "deploy.sh record-heads <dir> <spec_file>"
        spec="$1"
        # The image clones every repository inside its build (doodba's
        # ONBUILD gitaggregate, after ``COPY ./custom``), so the build
        # context alone decides whether Docker reuses its cache: with
        # nothing in it changed, a branch that moved was not cloned again
        # and a push went live as the old code (5-nov-2026). This file,
        # inside that context, names the commit each branch points at
        # now: a branch that moved changes it and the build clones again;
        # with none moved the build stays cached.
        #
        # <spec_file>: one ``<alias>TAB<kind>TAB<ref>TAB<url>`` line per
        # merge of repos.yaml, with the same URL (and token) the build
        # clones from. <kind> is ``pin`` for a commit the panel pinned,
        # its own head; ``ref`` for a branch or tag, asked to the remote.
        # The executor says which, from where the ref came: a branch can
        # be named like a commit (``cafebabe``, ``20261105``). Neither URL
        # nor git's own messages are printed: they carry the token.
        heads="$dir/odoo/custom/incubacloud-heads.txt"
        tmp_heads="$heads.ic-tmp"
        : > "$tmp_heads"
        # git asks from a HOME of its own, without the system's config,
        # so no credential helper of the host's user (store, cache,
        # libsecret, a credential manager) is ever handed the token
        # (5-nov-2026). Variables rather than GIT_CONFIG_GLOBAL, which
        # git before 2.32 ignores.
        git_home="$(mktemp -d)"
        trap 'rm -rf "$git_home"' EXIT
        timed_out=""
        while IFS=$'\t' read -r alias kind ref url; do
            [ -n "$alias" ] || continue
            sha=""
            if [ "$kind" = pin ]; then
                sha="$ref"
            elif [ -n "$timed_out" ]; then
                # One remote that did not answer in time: the rest are
                # not waited for, the build clones them all again anyway.
                :
            elif [ -n "$url" ]; then
                rc=0
                sha="$(remote_ref_head "$git_home" "$url" "$ref")" || rc=$?
                # 124: timeout's own; 137: git ignored TERM and -k's KILL
                # ended it.
                if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
                    timed_out=1
                    ic_warn "the head of $alias ($ref) did not arrive within ${HEADS_TIMEOUT}s: the rest are not asked"
                fi
            fi
            if [ -z "$sha" ]; then
                # Unknown is never taken as unchanged: a value of its own
                # makes this build clone every repository again, which
                # is slower but cannot ship old code. If GitHub is really
                # out of reach, that clone fails the build instead.
                sha="unknown-$(date +%s%N)-$$-$RANDOM"
                ic_warn "could not read the head of $alias ($ref): the image is built again without the cache of its repositories"
            fi
            ic_log "head of $alias ($ref): $sha"
            printf '%s %s %s\n' "$alias" "$ref" "$sha" >> "$tmp_heads"
        done < "$spec"
        mv "$tmp_heads" "$heads"
        # Rewritten by every build: kept out of the instance's history.
        ic_git_exclude "$dir" odoo/custom/incubacloud-heads.txt
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
