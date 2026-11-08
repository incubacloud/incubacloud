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
#   deploy.sh record-heads       <dir> <spec_file> [helper_image]
#   deploy.sh git-trust          <dir> <spec_file>
#   deploy.sh fetch-repos        <dir> <spec_file> <helper_image> [retry_wait]
#   deploy.sh debian-archive     <dir>
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

# Seconds set-system-params waits for ``db`` to accept connections. A
# rebuild and a plan change run it right after ``docker compose up -d``
# (``--remove-orphans``), which may have just recreated ``db``, and
# nothing in between waits for it: ``odoo`` depends on ``db`` without
# ``condition: service_healthy``. psql then met "the database system is
# starting up" about once in 65 recreations and failed a job whose change
# was already on the host (QA, 7-nov-2026). A recreated database answers
# within seconds; 60 is the bound every other wait on ``db`` uses
# (move_steps.sh, the tenant rebuild's parked database).
# IC_DB_WAIT_SECONDS overrides it, so a test need not wait a minute.
DB_WAIT_SECONDS="${IC_DB_WAIT_SECONDS:-60}"

# Seconds one repository's download may take in fetch-repos: Odoo 8.0's
# tree at depth 1 is hundreds of MB, and a network failure must not hold
# the job (8-nov-2026). IC_FETCH_TIMEOUT overrides it, for the tests.
FETCH_TIMEOUT="${IC_FETCH_TIMEOUT:-600}"

# The git helper image (``alpine/git`` by digest, the executor's
# GIT_HELPER_IMAGE), for record-heads with a pinned address and for
# fetch-repos. Empty: no helper container is run.
GIT_HELPER_IMAGE=""

# Set by fetch-repos: every git call goes through the helper container,
# with redirects refused for GitHub too (8.0 to 10.0, plan-gitlab.md
# §4.4-8).
IC_GIT_ALL_IN_CONTAINER=""

# The helper container running now, removed on exit, hang-up or
# termination; and the temporary directories to remove on exit.
IC_GIT_CONTAINER=""
IC_TMP_DIRS=()

# A label naming the instance directory's job, on every helper container:
# the next run of a step on that directory removes those a killed run left.
IC_JOB_TOKEN="$(printf '%s' "$dir" | sha256sum | cut -c1-16)"

# Remove the helper container in flight and the temporary directories.
ic_git_cleanup() {
    local tmp
    if [ -n "$IC_GIT_CONTAINER" ]; then
        docker rm -f "$IC_GIT_CONTAINER" >/dev/null 2>&1 || true
        IC_GIT_CONTAINER=""
    fi
    for tmp in "${IC_TMP_DIRS[@]}"; do
        rm -rf "${tmp:?}"
    done
}

# Run the cleanup on exit, and on a hang-up or a termination: cancelling
# a job, or the transport's timeout, kills the ``docker run`` client but
# not the container, which would go on writing on the host. A SIGKILL
# cannot be trapped: the next run's ic_git_reap covers it.
ic_git_traps() {
    trap ic_git_cleanup EXIT
    trap 'ic_git_cleanup; exit 129' HUP
    trap 'ic_git_cleanup; exit 143' TERM
}

# Make an owner-only directory for git's HOME and its token, named after
# this directory's job so the next run can sweep what a SIGKILL left
# (ic_git_sweep), and set IC_TMP to it (a variable: an array appended to
# in a command substitution is lost). Removed on exit.
ic_git_tmpdir() {
    IC_TMP="$(mktemp -d "${TMPDIR:-/tmp}/ic-git-$IC_JOB_TOKEN-XXXXXXXX")"
    IC_TMP_DIRS+=("$IC_TMP")
}

# Remove the temporary HOMEs, with their token, that a killed earlier run
# on this directory left (a SIGKILL cannot be trapped).
ic_git_sweep() {
    local tmp
    for tmp in "${TMPDIR:-/tmp}/ic-git-$IC_JOB_TOKEN-"*; do
        [ -e "$tmp" ] || continue
        rm -rf "${tmp:?}"
    done
}

# Remove the helper containers an earlier run on this directory left.
ic_git_reap() {
    local ids=()
    mapfile -t ids < <(docker ps -aq --filter "label=ic.job=$IC_JOB_TOKEN" 2>/dev/null || true)
    if [ "${#ids[@]}" -gt 0 ]; then
        docker rm -f "${ids[@]}" >/dev/null 2>&1 || true
    fi
}

# Run the helper image with its own HOME, and print what it prints.
#
# Usage: ic_git_container <seconds> <home> <workdir|-> <add_host|-> <entry> [args...]
#   <home>      an owner-only directory, mounted read-only as git's HOME
#   <workdir>   mounted read-write as /w and made the working directory
#   <add_host>  ``host:ip`` pinned in the container's /etc/hosts
#   <entry>     the image's command: git or sh
#
# Run in the background and waited for, so a hang-up or a termination
# reaches the trap at once, not when the download ends. Returns the
# command's status, 124 (137) when it ran out of time, in which case the
# container is removed: killing ``docker run`` does not stop it.
ic_git_container() {
    local secs="$1" home="$2" work="$3" add_host="$4" entry="$5" name rc=0
    shift 5
    name="ic-git-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
    local args=(run --rm --name "$name" --label "ic.job=$IC_JOB_TOKEN"
        --user "$(id -u):$(id -g)" -v "$home:/h:ro" -e HOME=/h
        -e GIT_CONFIG_NOSYSTEM=1 -e GIT_TERMINAL_PROMPT=0)
    [ "$work" = - ] || args+=(-v "$work:/w" -w /w)
    [ "$add_host" = - ] || args+=(--add-host "$add_host")
    args+=(--entrypoint "$entry" "$GIT_HELPER_IMAGE" "$@")
    IC_GIT_CONTAINER="$name"
    timeout -k 5 "$secs" docker "${args[@]}" &
    wait "$!" || rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        docker rm -f "$name" >/dev/null 2>&1 || true
    fi
    IC_GIT_CONTAINER=""
    return "$rc"
}

# Write git's config for one repository into <home>, and print the URL
# without its credentials.
#
# Usage: ic_git_home <home> <url> <cred_authority> <ca_file> <in_container>
#
# The credentials of an https URL travel as an ``Authorization`` header in
# that HOME's config, not on git's command line, which every user of the
# host can read; one HOME per repository, so a container sees its own
# token alone. They go only to <cred_authority> (empty: github.com),
# exactly, over https, and not at all with a ? # or \ before the path
# (5-nov-2026); off GitHub, a URL without them must name that
# server over https too. Off GitHub (and for every URL in fetch-repos)
# redirects are refused, and a server's own CA (<ca_file>) is trusted for
# its URLs alone. Returns 1, writing nothing, for a URL refused.
ic_git_home() {
    local home="$1" url="$2" cred_authority="${3:-github.com}" ca_file="$4"
    local in_container="$5" auth="" authority bare ca_ref=""
    authority="${url#*://}"
    authority="${authority%%/*}"
    bare="${authority##*@}"
    # A ? # or \ before the first / may end the host before the last @,
    # as in ``TOK@evil.example?@github.com/x``, whose host is evil.example
    # for curl: parsers do not agree on it, so credentials with one of
    # them there are not sent at all (5-nov-2026).
    if [[ "$authority" == *@* ]] \
        && [[ "$url" != https://* || "$bare" != "$cred_authority" \
            || "$authority" == *[?#\\]* ]]; then
        return 1
    fi
    if [ "$cred_authority" != github.com ] \
        && [[ "$url" != https://* || "$bare" != "$cred_authority" \
            || "$authority" == *[?#\\]* ]]; then
        return 1
    fi
    # Percent-encoded credentials stay in the URL, as git needs them
    # decoded in a header and this does not decode (5-nov-2026).
    if [[ "$url" =~ ^(https://)([^/@]+)@(.+)$ ]] \
        && [[ "${BASH_REMATCH[2]}" != *%* ]]; then
        auth="$(printf '%s' "${BASH_REMATCH[2]}" | base64 -w0)"
        url="${BASH_REMATCH[1]}${BASH_REMATCH[3]}"
    fi
    rm -f "${home:?}/ca.pem"
    if [ -n "$ca_file" ]; then
        if [ -n "$in_container" ]; then
            install -m 0600 "$ca_file" "$home/ca.pem"
            ca_ref=/h/ca.pem
        else
            ca_ref="$ca_file"
        fi
    fi
    (
        umask 077
        {
            if [ -n "$auth" ]; then
                # Scoped to the server it is for, never global: a
                # redirect or a submodule elsewhere does not get it.
                printf '[http "https://%s/"]\n\textraHeader = Authorization: Basic %s\n' \
                    "$cred_authority" "$auth"
            fi
            if [ -n "$IC_GIT_ALL_IN_CONTAINER" ]; then
                printf '[http]\n\tfollowRedirects = false\n'
            fi
            if [ "$cred_authority" != github.com ]; then
                printf '[http "https://%s/"]\n\tfollowRedirects = false\n' \
                    "$cred_authority"
                if [ -n "$ca_ref" ]; then
                    printf '\tsslCAInfo = %s\n' "$ca_ref"
                fi
            fi
        } > "$home/.gitconfig"
    )
    printf '%s\n' "$url"
}

# Split one line of a heads spec on its tabs into IC_COLS, keeping empty
# columns: ``read`` with a tab in IFS takes a run of tabs for one.
#
# Usage: ic_split_tabs <line>
ic_split_tabs() {
    local line="$1"
    IC_COLS=()
    while [[ "$line" == *$'\t'* ]]; do
        IC_COLS+=("${line%%$'\t'*}")
        line="${line#*$'\t'}"
    done
    IC_COLS+=("$line")
    while [ "${#IC_COLS[@]}" -lt 7 ]; do
        IC_COLS+=("")
    done
}

# Set IC_HEAD to the commit(s) <ref> names at <url>, space-separated, or
# nothing. A variable, not printed: a helper container must run in this
# shell, not in a command substitution, for the traps to remove it.
#
# Usage: remote_ref_head <git_home> <url> <ref> [cred_authority] [ca_file] [pin_ip]
#   <git_home>  an empty owner-only directory, git's HOME for the call
#   <url>       the repository, as repos.yaml names it (token included)
#   <ref>       a branch or tag name, or a full ``refs/...`` name
#   <cred_authority>, <ca_file>: see ic_git_home
#   <pin_ip>    the address the panel checked for the server: git then
#               runs in the helper container with ``--add-host``, never
#               with the host's git, whose version may ignore a pin
#               (Ubuntu 22.04's 2.34 drops http.curloptResolve silently)
#
# A plain name is asked exactly as ``refs/heads/<ref>`` and
# ``refs/tags/<ref>``: a bare pattern also matches ``refs/heads/x/main``
# or ``refs/pull/*/head``. Both are kept when both exist, since a
# fetch prefers the tag. git runs without the host's configuration (see
# record-heads), says nothing, and asks nothing. Returns git's status,
# 124 (137 if KILL ended it) when it ran out of time, 1 for a URL
# refused (ic_git_home) or a pin with no helper image.
remote_ref_head() {
    local home="$1" url="$2" ref="$3" cred_authority="${4:-github.com}"
    local ca_file="${5:-}" pin_ip="${6:-}" out in_container="" add_host=-
    local out_file rc=0
    local wanted=("refs/heads/$ref" "refs/tags/$ref")
    IC_HEAD=""
    [[ "$ref" != refs/* ]] || wanted=("$ref")
    if [ -n "$pin_ip" ] || [ -n "$IC_GIT_ALL_IN_CONTAINER" ]; then
        [ -n "$GIT_HELPER_IMAGE" ] || return 1
        in_container=1
        [ -z "$pin_ip" ] || add_host="${cred_authority%:*}:$pin_ip"
    fi
    url="$(ic_git_home "$home" "$url" "$cred_authority" "$ca_file" "$in_container")" \
        || return 1
    if [ -n "$in_container" ]; then
        out_file="$home/.ic-out"
        ic_git_container "$HEADS_TIMEOUT" "$home" - "$add_host" git \
            -c credential.helper= -c protocol.version=2 \
            ls-remote -- "$url" "${wanted[@]}" > "$out_file" 2>/dev/null || rc=$?
        out="$(cat "$out_file")"
        rm -f "$out_file"
        [ "$rc" -eq 0 ] || return "$rc"
    else
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
    fi
    IC_HEAD="$(awk -v a="${wanted[0]}" -v b="${wanted[1]:-}" \
        '$2 == a || ($2 == b && b != "") {print $1}' <<< "$out" \
        | sort -u | paste -sd ' ')"
}

# Resolve the head of every line of a heads spec into <heads_file>, one
# ``<alias> <ref> <sha>`` line each.
#
# Usage: ic_resolve_heads <spec_file> <heads_file>
#
# <spec_file>: one ``<alias>TAB<kind>TAB<ref>TAB<url>`` line per merge of
# repos.yaml, with the same URL (and token) the build clones from, and
# optionally three more columns: ``<cred_authority>TAB<ca_file>TAB<pin_ip>``
# (a line of four is read as before, a GitHub one). <kind> is ``pin`` for
# a commit the panel pinned, its own head; ``ref`` for a branch or tag,
# asked to the remote. Neither URL nor git's own messages are printed:
# they carry the token.
ic_resolve_heads() {
    local spec="$1" heads="$2" tmp_heads git_home sha rc server
    local alias kind ref url cred_authority ca_file pin_ip line
    local -A timed_out=()
    tmp_heads="$heads.ic-tmp"
    : > "$tmp_heads"
    # git asks from a HOME of its own, without the system's config,
    # so no credential helper of the host's user (store, cache,
    # libsecret, a credential manager) is ever handed the token
    # (5-nov-2026). Variables rather than GIT_CONFIG_GLOBAL, which
    # git before 2.32 ignores.
    ic_git_tmpdir
    git_home="$IC_TMP"
    while IFS= read -r line; do
        ic_split_tabs "$line"
        alias="${IC_COLS[0]}" kind="${IC_COLS[1]}" ref="${IC_COLS[2]}"
        url="${IC_COLS[3]}" cred_authority="${IC_COLS[4]}"
        ca_file="${IC_COLS[5]}" pin_ip="${IC_COLS[6]}"
        [ -n "$alias" ] || continue
        sha=""
        server="${url#*://}"
        server="${server%%/*}"
        server="s:${server##*@}"
        if [ "$kind" = pin ]; then
            sha="$ref"
        elif [ -n "${timed_out[$server]+x}" ]; then
            # A server that did not answer in time is not waited for
            # again; the other servers still are, so one slow GitLab
            # does not send odoo's download again (8-nov-2026).
            :
        elif [ -n "$url" ]; then
            rc=0
            remote_ref_head "$git_home" "$url" "$ref" \
                "${cred_authority:-github.com}" "$ca_file" "$pin_ip" || rc=$?
            sha="$IC_HEAD"
            # 124: timeout's own; 137: git ignored TERM and -k's KILL
            # ended it.
            if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
                timed_out[$server]=1
                ic_warn "the head of $alias ($ref) did not arrive within ${HEADS_TIMEOUT}s: the rest of its server are not asked"
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
}

# The directory of the markers naming the commit each fetched tree is at,
# one file per alias. Outside the trees: a repository is what the host
# writes them from, and a ``.incubacloud-head`` symbolic link in one would
# have had the host write through it (security review of G1, 8-nov-2026).
# Its ``.ic-`` name keeps it out of the build context and of the
# instance's history with the temporary trees.
#
# Usage: ic_heads_dir <src>
ic_heads_dir() {
    printf '%s/.ic-heads' "${1:?}"
}

# Print the commit <src>'s marker for <alias> names, or nothing when there
# is none or it is not a plain file.
#
# Usage: ic_marker_read <src> <alias>
ic_marker_read() {
    local marker
    marker="$(ic_heads_dir "$1")/${2:?}"
    if [ -f "$marker" ] && [ ! -L "$marker" ]; then
        head -c 64 "$marker"
    fi
}

# Remove <src>'s markers' directory when it is a link or not a
# directory: it is never read or written through.
#
# Usage: ic_heads_dir_unlink <src>
ic_heads_dir_unlink() {
    local dir
    dir="$(ic_heads_dir "$1")"
    if [ -L "$dir" ] || { [ -e "$dir" ] && [ ! -d "$dir" ]; }; then
        rm -f "${dir:?}"
    fi
}

# Return whether <alias> names what doodba keeps in custom/src for
# itself: its private addons and its repos and addons files, which a
# tree of that name would replace (doodbalib reads .yaml or .yml).
#
# Usage: ic_reserved_alias <alias>
ic_reserved_alias() {
    case "$1" in
        private|repos.yaml|repos.yml|addons.yaml|addons.yml) return 0 ;;
    esac
    return 1
}

# Write <sha> as <src>'s marker for <alias>, replacing whatever entry is
# there (a rename replaces a link, never writes through it); the
# directory is made again when it is not a plain one.
#
# Usage: ic_marker_write <src> <alias> <sha>
ic_marker_write() {
    local dir tmp
    dir="$(ic_heads_dir "$1")"
    ic_heads_dir_unlink "$1"
    mkdir -p "$dir"
    tmp="$(mktemp "$dir/.tmp-XXXXXXXX")"
    printf '%s\n' "$3" > "$tmp"
    mv -fT "$tmp" "$dir/${2:?}"
}

# Download one repository at one commit into <src>/<alias>, through a
# temporary tree in the same file system that is renamed into place as
# the last step. Sets IC_FETCHED to the commit that arrived (a variable:
# see remote_ref_head).
#
# Usage: ic_fetch_one <src> <alias> <ref> <url> <cred_authority> <ca_file> <pin_ip> <sha>
#   <sha>  the head resolved for it; ``unknown-…`` fetches <ref> by name
#
# The tree that was there stays until the new one is complete; its
# marker (ic_heads_dir) goes first and the new one is written last, so a
# run cut anywhere leaves no tree with a marker it does not match.
# Returns 0, or the download's status (3: the commit that arrived is not
# <sha>; 4: the server redirects; 5: the URL is refused, see ic_git_home;
# 124 or 137: out of time).
ic_fetch_one() {
    local src="$1" alias="$2" ref="$3" url="$4" cred_authority="$5"
    local ca_file="$6" pin_ip="$7" sha="$8" tmp home add_host=- target expect
    local got rc=0 out_file
    IC_FETCHED=""
    ic_git_tmpdir
    home="$IC_TMP"
    url="$(ic_git_home "$home" "$url" "${cred_authority:-github.com}" "$ca_file" 1)" \
        || return 5
    tmp="${src:?}/.ic-tmp-${alias:?}"
    rm -rf "${tmp:?}"
    mkdir -p "$tmp"
    [ -z "$pin_ip" ] || add_host="${cred_authority%:*}:$pin_ip"
    target="$sha"
    expect="$sha"
    if [[ "$sha" == unknown-* ]]; then
        target="$ref"
        expect=""
    fi
    out_file="$home/.ic-out"
    # shellcheck disable=SC2016  # expanded by the container's shell
    ic_git_container "$FETCH_TIMEOUT" "$home" "$tmp" "$add_host" sh -c '
        set -e
        git init -q .
        if ! git -c credential.helper= -c protocol.version=2 \
            fetch -q --depth=1 --no-tags -- "$1" "$2" 2>.ic-err; then
            grep -qE "error: 30[1278]" .ic-err && exit 4
            exit 2
        fi
        head="$(git rev-parse FETCH_HEAD)"
        [ -z "$3" ] || [ "$head" = "$3" ] || exit 3
        git checkout -q --detach FETCH_HEAD
        rm -rf .git .ic-err
        echo "$head"
    ' sh "$url" "$target" "$expect" > "$out_file" 2>/dev/null || rc=$?
    got="$(cat "$out_file")"
    rm -f "$out_file"
    if [ "$rc" -eq 0 ] && [ -z "$got" ]; then
        rc=2
    fi
    if [ "$rc" -ne 0 ]; then
        rm -rf "${tmp:?}"
        return "$rc"
    fi
    rm -f "$(ic_heads_dir "$src")/${alias:?}"
    if [ -e "$src/$alias" ] || [ -L "$src/$alias" ]; then
        rm -rf "${src:?}/${alias:?}"
    fi
    mv "$tmp" "$src/$alias"
    ic_marker_write "$src" "$alias" "$got"
    IC_FETCHED="$got"
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

    debian-archive)
        # The series' image is built on a Debian release gone from the
        # mirrors (its card's ``debian_archive``): doodba runs the files
        # of ``odoo/custom/build.d`` with its own in name order, and 05
        # comes before every one that uses apt: the template's
        # 20-update-pg-repos and doodba's 200-dependencies.
        # Written again by every deploy and rebuild: copier does not own
        # the file, and a new version of it reaches the image this way.
        build_d="$dir/odoo/custom/build.d"
        mkdir -p "$build_d"
        install -m 0755 "$(dirname "$0")/doodba/debian_archive.sh" \
            "$build_d/05-incubacloud-debian-archive"
        # And Let's Encrypt's new roots, which jessie's ca-certificates
        # does not have (plan-gitlab.md §4.4-10).
        install -m 0755 "$(dirname "$0")/doodba/isrg_roots.sh" \
            "$build_d/06-incubacloud-isrg-roots"
        ic_log "the image's apt will read Freexian's Extended LTS"
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
        # hostname regex constraint). A rebuild runs it after the stack
        # is up again, ``db`` perhaps just recreated: it is waited for
        # first (see DB_WAIT_SECONDS).
        cd "$dir"
        timeout "$DB_WAIT_SECONDS" sh -c \
            'until docker compose exec -T db pg_isready -q; do sleep 1; done' \
            || ic_die "db did not accept connections within ${DB_WAIT_SECONDS} s"
        docker compose exec -T db psql -U "$pg_user" -d "$dbname" -c \
"DO \$\$ BEGIN IF EXISTS (SELECT FROM information_schema.tables WHERE \
table_schema='public' AND table_name='ir_config_parameter') THEN \
INSERT INTO ir_config_parameter (key,value) VALUES \
('web.base.url','$base_url_sql'),('report.url','$report_url') \
ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value; END IF; END \$\$;"
        ;;

    record-heads)
        ic_require_args 1 "$#" "deploy.sh record-heads <dir> <spec_file> [helper_image]"
        spec="$1"
        GIT_HELPER_IMAGE="${2:-}"
        # The image clones every repository inside its build (doodba's
        # ONBUILD gitaggregate, after ``COPY ./custom``), so the build
        # context alone decides whether Docker reuses its cache: with
        # nothing in it changed, a branch that moved was not cloned again
        # and a push went live as the old code (5-nov-2026). This file,
        # inside that context, names the commit each branch points at
        # now: a branch that moved changes it and the build clones again;
        # with none moved the build stays cached. See ic_resolve_heads
        # for the spec; a line with a pinned address is asked from the
        # helper container (<helper_image>).
        heads="$dir/odoo/custom/incubacloud-heads.txt"
        ic_git_traps
        # The containers a killed run left go before its HOMEs: one still
        # running reads its token from the HOME it mounts.
        [ -z "$GIT_HELPER_IMAGE" ] || ic_git_reap
        ic_git_sweep
        ic_resolve_heads "$spec" "$heads"
        # Rewritten by every build: kept out of the instance's history.
        ic_git_exclude "$dir" odoo/custom/incubacloud-heads.txt
        ;;

    git-trust)
        ic_require_args 1 "$#" "deploy.sh git-trust <dir> <spec_file>"
        spec="$1"
        # What the image's git needs to clone from a Git provider other
        # than GitHub (11.0 to 19.0, which clone inside the build): no
        # redirect followed for that server, and its own CA trusted for
        # its URLs alone. doodba runs ``odoo/custom/build.d`` in name
        # order with its own, and 060 comes before 100-repos-aggregate
        # (checked on 11.0 and 19.0, 8-nov-2026). Written again by every
        # deploy and rebuild; with no such server, both go.
        build_d="$dir/odoo/custom/build.d"
        pem_dir="$dir/odoo/custom/incubacloud-git"
        step="$build_d/060-incubacloud-git"
        ic_git_exclude "$dir" odoo/custom/build.d/060-incubacloud-git
        ic_git_exclude "$dir" odoo/custom/incubacloud-git/
        declare -A ca_of=()
        authorities=()
        while IFS= read -r line; do
            ic_split_tabs "$line"
            cred_authority="${IC_COLS[4]}"
            ca_file="${IC_COLS[5]}"
            if [ -z "$cred_authority" ] || [ "$cred_authority" = github.com ]; then
                continue
            fi
            [[ "$cred_authority" =~ ^[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] \
                || ic_die "invalid Git server in the spec: $cred_authority"
            if [ -z "${ca_of[$cred_authority]+x}" ]; then
                authorities+=("$cred_authority")
                ca_of[$cred_authority]="$ca_file"
            fi
        done < "$spec"
        rm -rf "${pem_dir:?}"
        rm -f "${step:?}"
        if [ "${#authorities[@]}" -eq 0 ]; then
            ic_log "no Git server besides GitHub: the image's git is left as it is"
            exit 0
        fi
        mkdir -p "$build_d" "$pem_dir"
        tmp_step="$step.ic-tmp"
        {
            printf '#!/bin/sh\n'
            printf '# Written by IncubaCloud on every deploy and rebuild.\n'
            printf 'set -e\n'
            for authority in "${authorities[@]}"; do
                printf 'git config --system http.https://%s/.followRedirects false\n' \
                    "$authority"
                if [ -n "${ca_of[$authority]}" ]; then
                    pem="${authority//:/_}.pem"
                    install -m 0644 "${ca_of[$authority]}" "$pem_dir/$pem"
                    printf 'git config --system http.https://%s/.sslCAInfo /opt/odoo/custom/incubacloud-git/%s\n' \
                        "$authority" "$pem"
                fi
            done
            printf 'echo "[incubacloud] the image trusts its Git servers"\n'
        } > "$tmp_step"
        chmod 0755 "$tmp_step"
        mv "$tmp_step" "$step"
        ic_log "the image's git refuses redirects for ${#authorities[@]} Git server(s)"
        ;;

    fetch-repos)
        ic_require_args 2 "$#" \
            "deploy.sh fetch-repos <dir> <spec_file> <helper_image> [retry_wait]"
        spec="$1"
        GIT_HELPER_IMAGE="$2"
        retry_wait="${3:-60}"
        # 8.0 to 10.0 (plan-gitlab.md §4.4-8): the image's git (2.1.4)
        # sends a token in the clear after a 302 to http on the same
        # host and knows no http.followRedirects (spike of 8-nov-2026),
        # so it touches no repository. Every repository of the spec is
        # fetched here, at one commit, by the helper container, into
        # odoo/custom/src/<alias>, and the image builds with
        # AGGREGATE=false. See ic_resolve_heads for the spec; ``pin``
        # lines carry their URL too.
        src="$dir/odoo/custom/src"
        [ -d "$src" ] || ic_die "build context not found: $src"
        IC_GIT_ALL_IN_CONTAINER=1
        ic_git_traps
        # 1. Before fetching anything: what a run cut short left, and the
        #    exclusions from the instance's history, so that a job killed
        #    between moving a tree and excluding it never lets
        #    ``rebuild.sh commit-dirty`` commit the trees. The containers
        #    first: one still running would write into a temporary tree
        #    removed under it, or read its token from a HOME removed.
        ic_git_reap
        ic_git_sweep
        for leftover in "$src"/.ic-tmp-*; do
            [ -e "$leftover" ] || [ -L "$leftover" ] || continue
            rm -rf "${leftover:?}"
        done
        # The markers' directory is never read or written through a
        # link, and the temporary markers of a killed ic_marker_write,
        # which the removal of dropped trees does not see, go too.
        heads_dir="$(ic_heads_dir "$src")"
        ic_heads_dir_unlink "$src"
        if [ -d "$heads_dir" ]; then
            rm -f "$heads_dir"/.tmp-*
        fi
        declare -A seen=()
        rows=()
        while IFS= read -r line; do
            ic_split_tabs "$line"
            alias="${IC_COLS[0]}"
            [ -n "$alias" ] || continue
            if [[ ! "$alias" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] \
                || ic_reserved_alias "$alias"; then
                ic_die "invalid repository alias: $alias"
            fi
            [ -z "${seen[$alias]+x}" ] \
                || ic_die "$alias has several merges: on 8.0 to 10.0 each repository is fetched at a single commit"
            seen[$alias]=1
            rows+=("$line")
        done < "$spec"
        for row in "${rows[@]}"; do
            ic_git_exclude "$dir" "odoo/custom/src/${row%%$'\t'*}/"
        done
        ic_git_exclude "$dir" 'odoo/custom/src/.ic-*'
        ic_git_exclude "$dir" odoo/Dockerfile.dockerignore
        ic_git_exclude "$dir" odoo/custom/incubacloud-heads.txt
        # 2. The heads, as record-heads reads them.
        heads="$dir/odoo/custom/incubacloud-heads.txt"
        ic_resolve_heads "$spec" "$heads"
        declare -A head_of=()
        while read -r alias _ref sha; do
            head_of[$alias]="$sha"
        done < "$heads"
        # 3. Each repository whose tree is not at its head already.
        fetched=0
        for row in "${rows[@]}"; do
            ic_split_tabs "$row"
            alias="${IC_COLS[0]}" ref="${IC_COLS[2]}" url="${IC_COLS[3]}"
            cred_authority="${IC_COLS[4]}" ca_file="${IC_COLS[5]}"
            pin_ip="${IC_COLS[6]}"
            sha="${head_of[$alias]}"
            if [[ "$sha" != unknown-* ]] && [ -d "$src/$alias" ] \
                && [ ! -L "$src/$alias" ] \
                && [ "$(ic_marker_read "$src" "$alias")" = "$sha" ]; then
                ic_log "$alias is at $sha already"
                continue
            fi
            attempt=1
            while :; do
                rc=0
                ic_fetch_one "$src" "$alias" "$ref" "$url" \
                    "$cred_authority" "$ca_file" "$pin_ip" "$sha" || rc=$?
                got="$IC_FETCHED"
                [ "$rc" -ne 0 ] || break
                if [ "$rc" -eq 4 ]; then
                    ic_die "$alias: the server redirects; update the repository's URL"
                fi
                if [ "$rc" -eq 5 ]; then
                    ic_die "$alias: its URL is refused: credentials go only to the server they are for, over https, and a URL off GitHub must name that server over https"
                fi
                if [ "$attempt" -ge 2 ]; then
                    ic_die "$alias: the download failed twice (status $rc)"
                fi
                ic_warn "$alias: the download failed (status $rc); trying again in ${retry_wait}s"
                sleep "$retry_wait"
                attempt=$((attempt + 1))
            done
            ic_log "$alias fetched at $got"
            fetched=$((fetched + 1))
            if [[ "$sha" == unknown-* ]]; then
                awk -v a="$alias" -v s="$sha" -v g="$got" \
                    '$1 == a && $3 == s {$3 = g} {print}' "$heads" > "$heads.ic-tmp"
                mv "$heads.ic-tmp" "$heads"
            fi
        done
        # Trees of repositories the instance no longer has (only those
        # this step wrote, which have a marker), from a markers'
        # directory that is a plain one: through a link, the files of
        # another directory would be taken for markers and removed.
        markers=()
        if [ -d "$heads_dir" ] && [ ! -L "$heads_dir" ]; then
            markers=("$heads_dir"/*)
        fi
        for marker in "${markers[@]}"; do
            [ -e "$marker" ] || [ -L "$marker" ] || continue
            name="${marker##*/}"
            if ic_reserved_alias "$name" || [ -n "${seen[$name]+x}" ]; then
                continue
            fi
            rm -f "${marker:?}"
            if [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] \
                && [ -d "$src/$name" ] && [ ! -L "$src/$name" ]; then
                rm -rf "${src:?}/${name:?}"
                ic_log "removed the tree of $name, no longer a repository of the instance"
            fi
        done
        # 4. The build context: the template's .dockerignore leaves
        #    custom/src/* out, so the fetched trees are let in by name.
        #    BuildKit prefers Dockerfile.dockerignore and copier still owns
        #    .dockerignore. The template's ``!custom/src/*.*`` would let
        #    the temporary trees in, hence the last line (8-nov-2026).
        ignore="$dir/odoo/Dockerfile.dockerignore"
        {
            if [ -f "$dir/odoo/.dockerignore" ]; then
                cat "$dir/odoo/.dockerignore"
                [ -z "$(tail -c 1 "$dir/odoo/.dockerignore")" ] || printf '\n'
            fi
            for row in "${rows[@]}"; do
                printf '!custom/src/%s\n' "${row%%$'\t'*}"
            done
            printf 'custom/src/.ic-*\n'
        } > "$ignore.ic-tmp"
        mv "$ignore.ic-tmp" "$ignore"
        ic_log "${#rows[@]} repositories ready for the build, $fetched fetched"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
