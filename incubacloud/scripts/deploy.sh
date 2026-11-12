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
#   deploy.sh fetch-repos        <dir> <spec_file> <helper_image> <series> [retry_wait] [budget]
#   deploy.sh debian-archive     <dir>
#
# The executor keeps each deploy step as its own labelled command (so
# subclasses of other modules can still splice by label); this script only
# carries the steps whose bodies were real shell logic. The trivial
# mv/rm/ln steps stay inline in the executor.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "deploy.sh <operation> <dir> ..."

op="$1"
# As given, for ic_fetch_lock, which finds an earlier run by its arguments.
IC_DIR_ARG="$2"
dir="$(ic_expand_home "$2")"
shift 2

# Seconds one ``git ls-remote`` may take in record-heads. GitHub answers
# in well under a second; a host that cannot reach it should not hold
# the build for minutes (5-nov-2026).
HEADS_TIMEOUT=20
# The same from the helper container, whose start counts too: a host
# busy building would otherwise take a slow start for a server that does
# not answer, and fetch every repository of it again (10-nov-2026).
HEADS_CONTAINER_TIMEOUT=40

# Seconds set-system-params waits for ``db`` to accept connections. A
# rebuild and a plan change run it right after ``docker compose up -d``
# (``--remove-orphans``), which may have just recreated ``db``, and
# nothing in between waits for it: ``odoo`` depends on ``db`` without
# ``condition: service_healthy``. psql then met "the database system is
# starting up" about once in 65 recreations and failed a job whose change
# was already on the host (QA, 7-nov-2026). A recreated database answers
# within seconds; 60 is the bound every other wait on ``db`` uses
# (move_steps.sh and the parked database of a layer's own rebuild).
# IC_DB_WAIT_SECONDS overrides it, so a test need not wait a minute.
DB_WAIT_SECONDS="${IC_DB_WAIT_SECONDS:-60}"

# Seconds one repository's download may take in fetch-repos: Odoo's tree
# at depth 1 is hundreds of MB, and a network failure must not hold the
# job (8-nov-2026). IC_FETCH_TIMEOUT overrides it, for the tests.
FETCH_TIMEOUT="${IC_FETCH_TIMEOUT:-600}"

# Seconds the whole of fetch-repos may take, unless the executor passes
# its own (its _FETCH_BUDGET_S): each repository has FETCH_TIMEOUT or
# what is left, and the step's timeout is a minute more as a second
# barrier (10-nov-2026). IC_FETCH_BUDGET overrides it, for the tests.
FETCH_BUDGET="${IC_FETCH_BUDGET:-1200}"

# Where fetch-repos keeps the trees it downloads, one per repository and
# commit, for every instance of the host's account: each instance's
# ``odoo/custom/src/<alias>`` is a copy made of hard links to one, so
# twenty instances at the same commit of Odoo hold one tree of it on the
# disk, not twenty (10-nov-2026: a host of 25 panels had 26 GB free, and
# Odoo 19.0's tree is 1.2 GB). The daily prune removes the trees no
# instance links (host_maintenance.yml). IC_GIT_TREES overrides it, for
# the tests.
IC_TREES="${IC_GIT_TREES:-$HOME/.incubacloud/git-trees}"

# The git helper image (the executor's GIT_HELPER_IMAGE, doodba's by
# digest), for record-heads with a pinned address and for fetch-repos,
# which run it under its local, protected tag (ic_git_helper_tag). Empty:
# no helper container is run.
GIT_HELPER_IMAGE=""

# Set by fetch-repos: every git call goes through the helper container,
# with redirects refused for GitHub too.
IC_GIT_ALL_IN_CONTAINER=""

# The helper container running now, removed on exit, hang-up or
# termination; the temporary directories to remove on exit; and the
# ``-e`` variables of the next helper container.
IC_GIT_CONTAINER=""
IC_TMP_DIRS=()
IC_GIT_ENV=()

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
# Usage: ic_git_container <seconds> <home> <workdir|-> <add_hosts|-> <entry> [args...]
#   <home>       an owner-only directory, mounted read-only as git's HOME
#   <workdir>    mounted read-write as /w and made the working directory
#   <add_hosts>  ``host:ip`` pinned in the container's /etc/hosts, several
#                separated by spaces
#   <entry>      the image's command: git or sh
# The variables of IC_GIT_ENV (``NAME=value``) are set in the container:
# never a credential, as ``docker inspect`` shows them.
#
# Run in the background and waited for, so a hang-up or a termination
# reaches the trap at once, not when the download ends. Returns the
# command's status, 124 (137) when it ran out of time, in which case the
# container is removed: killing ``docker run`` does not stop it.
ic_git_container() {
    local secs="$1" home="$2" work="$3" add_hosts="$4" entry="$5" name rc=0
    local pin var
    shift 5
    name="ic-git-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
    local args=(run --rm --name "$name" --label "ic.job=$IC_JOB_TOKEN"
        --user "$(id -u):$(id -g)" --cap-drop ALL
        --security-opt no-new-privileges --pids-limit 512
        -v "$home:/h:ro" -e HOME=/h
        -e GIT_CONFIG_NOSYSTEM=1 -e GIT_TERMINAL_PROMPT=0)
    for var in "${IC_GIT_ENV[@]}"; do
        args+=(-e "$var")
    done
    [ "$work" = - ] || args+=(-v "$work:/w" -w /w)
    if [ "$add_hosts" != - ]; then
        for pin in $add_hosts; do
            args+=(--add-host "$pin")
        done
    fi
    args+=(--entrypoint "$entry" "$GIT_HELPER_IMAGE" "$@")
    IC_GIT_CONTAINER="$name"
    # Without fetch-repos' lock (fd 9): a client that outlived this run
    # would keep the instance locked.
    "$IC_TIMEOUT" -k 5 "$secs" docker "${args[@]}" 9>&- &
    wait "$!" || rc=$?
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
        docker rm -f "$name" >/dev/null 2>&1 || true
    fi
    IC_GIT_CONTAINER=""
    return "$rc"
}

# Start git's config in <home>: what holds for every URL of the call.
# Redirects are refused for every server when every git call runs in the
# helper container (fetch-repos), and git has the identity a merge needs
# (``rebuild.sh``'s own).
#
# Usage: ic_git_home_begin <home>
ic_git_home_begin() {
    local home="$1"
    rm -f "${home:?}/.gitconfig" "$home"/ca-*.pem
    (
        umask 077
        {
            printf '[user]\n\tname = IncubaCloud\n\temail = system@incubacloud\n'
            if [ -n "$IC_GIT_ALL_IN_CONTAINER" ]; then
                printf '[http]\n\tfollowRedirects = false\n'
            fi
        } > "$home/.gitconfig"
    )
}

# Add to <home>'s git config what one URL needs, and print the URL without
# its credentials.
#
# Usage: ic_git_home_add <home> <url> <cred_authority> <ca_file> <in_container>
#
# The credentials of an https URL travel as an ``Authorization`` header in
# that HOME's config, not on git's command line, which every user of the
# host can read, nor in repos.yaml; one HOME per repository, so a
# container sees its own token alone. The header is scoped to that
# repository's URL (10-nov-2026: to the whole server before), so neither
# another repository of the same server nor a redirect or a submodule
# gets it. Credentials go only to <cred_authority> (empty: github.com),
# exactly, over https, and not at all with a ? # or \ before the path
# (5-nov-2026); off GitHub, a URL without them must name that server over
# https too. Off GitHub, redirects are refused for that server, and its
# own CA (<ca_file>) is trusted for its URLs alone. Returns 1, writing
# nothing, for a URL refused.
ic_git_home_add() {
    local home="$1" url="$2" cred_authority="${3:-github.com}" ca_file="$4"
    local in_container="$5" auth="" authority bare ca_ref="" user_info pem
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
    if [[ "$url" =~ ^(https://)([^/@]+)@(.+)$ ]]; then
        # Percent-encoded credentials are decoded, as git would before
        # sending them; the header carries them in base64.
        user_info="${BASH_REMATCH[2]}"
        url="${BASH_REMATCH[1]}${BASH_REMATCH[3]}"
        auth="$(printf '%b' "${user_info//%/\\x}" | base64 -w0)"
    fi
    # The URL names a section of git's config: nothing in it may end the
    # section's quotes or line.
    if [ -n "$auth" ] && [[ ! "$url" =~ ^https://[^[:space:]\"\\]+$ ]]; then
        return 1
    fi
    if [ -n "$ca_file" ]; then
        if [ -n "$in_container" ]; then
            pem="ca-${cred_authority//[^A-Za-z0-9.-]/_}.pem"
            install -m 0600 "$ca_file" "$home/$pem"
            ca_ref="/h/$pem"
        else
            ca_ref="$ca_file"
        fi
    fi
    (
        umask 077
        {
            if [ -n "$auth" ]; then
                # Scoped to the repository it is for, never global.
                printf '[http "%s"]\n\textraHeader = Authorization: Basic %s\n' \
                    "$url" "$auth"
            fi
            if [ "$cred_authority" != github.com ]; then
                printf '[http "https://%s/"]\n\tfollowRedirects = false\n' \
                    "$cred_authority"
                if [ -n "$ca_ref" ]; then
                    printf '\tsslCAInfo = %s\n' "$ca_ref"
                fi
            fi
        } >> "$home/.gitconfig"
    )
    printf '%s\n' "$url"
}

# Write git's config for one URL into <home>, and print the URL without
# its credentials: ic_git_home_begin, then ic_git_home_add.
#
# Usage: ic_git_home <home> <url> <cred_authority> <ca_file> <in_container>
ic_git_home() {
    ic_git_home_begin "$1"
    ic_git_home_add "$@"
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
# nothing, and IC_HEADS_SECS to the seconds it was given (HEADS_TIMEOUT,
# or HEADS_CONTAINER_TIMEOUT from the helper container). Variables, not
# printed: a helper container must run in this shell, not in a command
# substitution, for the traps to remove it.
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
    IC_HEADS_SECS="$HEADS_TIMEOUT"
    [[ "$ref" != refs/* ]] || wanted=("$ref")
    if [ -n "$pin_ip" ] || [ -n "$IC_GIT_ALL_IN_CONTAINER" ]; then
        [ -n "$GIT_HELPER_IMAGE" ] || return 1
        in_container=1
        [ -z "$pin_ip" ] || add_host="${cred_authority%:*}:$pin_ip"
    fi
    url="$(ic_git_home "$home" "$url" "$cred_authority" "$ca_file" "$in_container")" \
        || return 1
    if [ -n "$in_container" ]; then
        IC_HEADS_SECS="$HEADS_CONTAINER_TIMEOUT"
        out_file="$home/.ic-out"
        ic_git_container "$HEADS_CONTAINER_TIMEOUT" "$home" - "$add_host" git \
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
                "$IC_TIMEOUT" -k 5 "$HEADS_TIMEOUT" \
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
                ic_warn "the head of $alias ($ref) did not arrive within ${IC_HEADS_SECS}s: the rest of its server are not asked"
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
# have had the host write through it.
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

# Make <src>'s marker for <alias> a hard link to <head>, the marker of a
# tree of the host's cache, replacing whatever entry is there (a rename
# replaces a link, never writes through it); the directory is made again
# when it is not a plain one. The link is what tells the daily prune the
# cached tree is in use; on another file system it is a copy.
#
# Usage: ic_marker_link <src> <alias> <head>
ic_marker_link() {
    local dir tmp
    dir="$(ic_heads_dir "$1")"
    ic_heads_dir_unlink "$1"
    mkdir -p "$dir"
    tmp="$(mktemp "$dir/.tmp-XXXXXXXX")"
    ln -f "${3:?}" "$tmp" 2>/dev/null || cp -f "$3" "$tmp"
    mv -fT "$tmp" "$dir/${2:?}"
}

# Print the name of the host's cached tree for <lines>: their SHA-256.
# The lines are each merge's URL, without its credentials, and commit, and
# with several merges each one's ref too, so that one name always comes
# with one marker. The same repository at the same commits is the same
# tree, whatever instance or alias asks for it; the cache is the host
# account's, which deploys for one panel only.
#
# Usage: ic_tree_key <lines>
ic_tree_key() {
    printf '%s' "$1" | sha256sum | cut -c1-64
}

# Return whether <entry> is a complete cached tree whose marker says
# <marker>: a plain ``tree`` directory and a plain ``head`` file.
#
# Usage: ic_tree_ready <entry> <marker>
ic_tree_ready() {
    local entry="$1"
    [ -d "$entry" ] && [ ! -L "$entry" ] \
        && [ -d "$entry/tree" ] && [ ! -L "$entry/tree" ] \
        && [ -f "$entry/head" ] && [ ! -L "$entry/head" ] \
        && [ "$(head -c 64 "$entry/head")" = "$2" ]
}

# Put the cached tree <entry> in place as <src>/<alias>, a copy made of
# hard links (a plain copy on another file system, which it warns of),
# through a temporary tree in <src> renamed into place; then link its
# marker. The tree that was there stays until the new one is complete,
# and is renamed aside, not removed, just before the new one takes its
# name: a run cut while it is removed leaves the new tree in place
# (10-nov-2026). Its marker goes first, so a run cut anywhere
# leaves no tree with a marker it does not match. ``-T`` everywhere: a
# name taken meanwhile fails the copy or the rename, never nests a tree
# in it (M2). The trees are never written in place: a write would reach
# every instance that links them.
#
# Usage: ic_tree_link <src> <alias> <entry>
ic_tree_link() {
    local src="$1" alias="$2" entry="$3" tmp old
    tmp="${src:?}/.ic-tmp-${alias:?}"
    old="$tmp.old"
    IC_TMP_DIRS+=("$tmp" "$old")
    rm -rf "${tmp:?}" "${old:?}"
    # Its marker's change time keeps the daily prune off it meanwhile.
    touch "$entry/head"
    if ! cp -alT "$entry/tree" "$tmp" 2>/dev/null; then
        rm -rf "${tmp:?}"
        ic_warn "$alias: its tree could not be linked from the host's cache (another file system, or no room): it is copied, and takes its own disk"
        cp -aT "$entry/tree" "$tmp"
    fi
    rm -f "$(ic_heads_dir "$src")/${alias:?}"
    if [ -e "$src/$alias" ] || [ -L "$src/$alias" ]; then
        mv -T "$src/$alias" "$old"
    fi
    mv -T "$tmp" "$src/$alias"
    ic_marker_link "$src" "$alias" "$entry/head"
    rm -rf "${old:?}"
}

# Print <url> without its user and token.
#
# Usage: ic_strip_userinfo <url>
ic_strip_userinfo() {
    local url="$1"
    if [[ "$url" =~ ^(https?://)[^/@]*@(.*)$ ]]; then
        url="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    fi
    printf '%s' "$url"
}

# Take fetch-repos' lock on <src> (``.ic-fetch.lock``, on fd 9), with
# this run's PID in it: two runs on one instance would link the same
# alias at once. A run left over from an earlier job (its worker died, or
# root's OpenSSH ignored the TERM) is told to stop, once ic_git_reap has
# removed its container, and waited for; only a process that is
# ``deploy.sh fetch-repos`` on <dir> is signalled. The lock goes with the
# process that holds it (10-nov-2026).
#
# Usage: ic_fetch_lock <src>
ic_fetch_lock() {
    local lock="${1:?}/.ic-fetch.lock" old cmdline
    [ ! -L "$lock" ] || rm -f "$lock"
    exec 9>>"$lock"
    if ! flock -n 9; then
        old="$(head -c 16 "$lock" 2>/dev/null | tr -dc 0-9)"
        if [ -n "$old" ] && [ "$old" != "$$" ] && [ -r "/proc/$old/cmdline" ]; then
            cmdline="$(tr '\0' ' ' < "/proc/$old/cmdline" 2>/dev/null || true)"
            if [[ "$cmdline" == *" fetch-repos $dir "* \
                || "$cmdline" == *" fetch-repos $IC_DIR_ARG "* ]]; then
                ic_warn "an earlier download of this instance's repositories (PID $old) is still running: it is stopped"
                kill -TERM "$old" 2>/dev/null || true
            fi
        fi
        flock -w 60 9 \
            || ic_die "an earlier download of this instance's repositories did not stop"
    fi
    printf '%s\n' "$$" > "$lock"
}

# Hold the host's cache of trees shared (fd 8, ``$IC_TREES/.lock``) while
# a tree is checked, linked or stored in it: the daily prune takes it
# exclusive, and removes nothing while a job holds it (10-nov-2026).
# Never held through a download.
#
# Usage: ic_trees_lock / ic_trees_unlock
ic_trees_lock() {
    flock -s -w 300 8 || ic_die "the host's cache of trees stayed locked"
}
ic_trees_unlock() {
    flock -u 8
}

# Return whether every pinned merge of <alias> can still be read with its
# own credential, the condition to link its tree from the host's cache:
# a branch's head was read with it this same run, but a pinned commit is
# never asked, and a tree another instance downloaded would otherwise
# reach a repository whose access was taken away (10-nov-2026). One
# ``ls-remote`` of the pinned commit's name, whose answer does not
# matter, only its status; a public repository passes without
# credential.
#
# Usage: ic_pins_readable <alias>
#   reads ROWS and IDX_OF[<alias>], as ic_fetch_tree
ic_pins_readable() {
    local i pin_idx=()
    read -ra pin_idx <<< "${IDX_OF[$1]}"
    for i in "${pin_idx[@]}"; do
        ic_split_tabs "${ROWS[$i]}"
        [ "${IC_COLS[1]}" = pin ] || continue
        [ -n "${IC_COLS[3]}" ] || return 1
        if [ -z "$IC_ACCESS_HOME" ]; then
            ic_git_tmpdir
            IC_ACCESS_HOME="$IC_TMP"
        fi
        remote_ref_head "$IC_ACCESS_HOME" "${IC_COLS[3]}" "${IC_COLS[2]}" \
            "${IC_COLS[4]:-github.com}" "${IC_COLS[5]}" "${IC_COLS[6]}" \
            || return 1
    done
}

# Return whether every merge of <alias> that follows a branch or tag
# still names the commit read before its download (HEAD_AT), asked again
# with its own credential. With several merges the container tells only
# the commit their merge made, not where each merge stood: a head that
# moved during the download would leave newer code under the name of the
# heads read before it, for every instance that asks for them (M12,
# 10-nov-2026). A pinned merge does not move; a head that cannot be read
# counts as moved.
#
# Usage: ic_heads_unmoved <alias>
#   reads ROWS, HEAD_AT and IDX_OF[<alias>], as ic_fetch_tree
ic_heads_unmoved() {
    local i merge_idx=()
    read -ra merge_idx <<< "${IDX_OF[$1]}"
    for i in "${merge_idx[@]}"; do
        ic_split_tabs "${ROWS[$i]}"
        [ "${IC_COLS[1]}" != pin ] || continue
        [ -n "${IC_COLS[3]}" ] || return 1
        if [ -z "$IC_ACCESS_HOME" ]; then
            ic_git_tmpdir
            IC_ACCESS_HOME="$IC_TMP"
        fi
        remote_ref_head "$IC_ACCESS_HOME" "${IC_COLS[3]}" "${IC_COLS[2]}" \
            "${IC_COLS[4]:-github.com}" "${IC_COLS[5]}" "${IC_COLS[6]}" \
            || return 1
        [ "$IC_HEAD" = "${HEAD_AT[$i]}" ] || return 1
    done
}

# Undo fetch-repos in <dir>: remove the trees it put in place (those with
# a marker, never through a link), the markers, its temporary trees, the
# build's ignore file and its seconds. Nothing to do on an instance that
# never fetched.
#
# Usage: ic_unfetch <dir>
ic_unfetch() {
    local src="$1/odoo/custom/src" heads_dir marker name
    heads_dir="$(ic_heads_dir "$src")"
    ic_heads_dir_unlink "$src"
    if [ -d "$heads_dir" ]; then
        for marker in "$heads_dir"/*; do
            [ -e "$marker" ] || continue
            name="${marker##*/}"
            if [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] \
                && ! ic_reserved_alias "$name" \
                && [ -d "$src/$name" ] && [ ! -L "$src/$name" ]; then
                rm -rf "${src:?}/${name:?}"
            fi
        done
        rm -rf "${heads_dir:?}"
        ic_log "removed the repositories fetched on the host: the image clones them"
    fi
    for marker in "$src"/.ic-tmp-*; do
        [ -e "$marker" ] || [ -L "$marker" ] || continue
        rm -rf "${marker:?}"
    done
    rm -f "$1/odoo/Dockerfile.dockerignore" "$src/.ic-fetch-seconds" \
        "$src/.ic-fetch.lock"
}

# Download one repository of the spec, every merge of it, into a new tree
# of the host's cache (IC_TREES), with doodba's git-aggregator in the
# helper container: the command doodba's ``autoaggregate`` runs in the
# image, limited to that alias, from a copy of the instance's repos.yaml
# (no credential in it). Sets IC_FETCHED to the commit the tree is at and
# IC_ENTRY_TMP to the new entry, not in the cache yet (variables: see
# remote_ref_head).
#
# Usage: ic_fetch_tree <src> <alias> <seconds>
#   reads ROWS, the spec's lines, and IDX_OF[<alias>], those of <alias>
#
# The credential of each merge's URL goes in the container's HOME
# (ic_git_home_add), the only place it is; the files are made with umask
# 0027, the modes doodba's ``800-permissions`` leaves as they are, so the
# image's build does not copy the tree again into its layer; no ``.git``
# is kept. git-aggregator's output goes to a file, of which the last lines
# are shown when it fails: its URLs carry no credential, and the header
# is never printed.
# Returns 0, or the download's status (2: it failed; 3: the commit that
# arrived is not the one pinned; 4: the server redirects; 5: a URL is
# refused, see ic_git_home_add; 124 or 137: out of time).
ic_fetch_tree() {
    local src="$1" alias="$2" secs="$3" home tmp add_hosts="" i rc=0
    local url cred_authority ca_file pin_ip expect="" idx=()
    IC_FETCHED=""
    IC_ENTRY_TMP=""
    read -ra idx <<< "${IDX_OF[$alias]}"
    ic_git_tmpdir
    home="$IC_TMP"
    ic_git_home_begin "$home"
    for i in "${idx[@]}"; do
        ic_split_tabs "${ROWS[$i]}"
        url="${IC_COLS[3]}" cred_authority="${IC_COLS[4]:-github.com}"
        ca_file="${IC_COLS[5]}" pin_ip="${IC_COLS[6]}"
        ic_git_home_add "$home" "$url" "$cred_authority" "$ca_file" 1 \
            >/dev/null || return 5
        [ -z "$pin_ip" ] || add_hosts+=" ${cred_authority%:*}:$pin_ip"
        if [ "${#idx[@]}" -eq 1 ] && [ "${IC_COLS[1]}" = pin ]; then
            expect="${IC_COLS[2]}"
        fi
    done
    add_hosts="${add_hosts# }"
    # The account's own: private repositories' code is kept there.
    (umask 077 && mkdir -p "$IC_TREES")
    tmp="$(mktemp -d "$IC_TREES/.tmp-$IC_JOB_TOKEN-XXXXXXXX")"
    IC_TMP_DIRS+=("$tmp")
    cp "$src/repos.yaml" "$tmp/repos.yaml"
    IC_GIT_ENV=("ODOO_VERSION=$SERIES" DEPTH_DEFAULT=1 DEPTH_MERGE=100)
    # shellcheck disable=SC2016  # expanded by the container's shell
    ic_git_container "$secs" "$home" "$tmp" "${add_hosts:--}" sh -c '
        set -e
        umask 0027
        if ! gitaggregate --no-color --expand-env --config repos.yaml \
            --log-level INFO --jobs 1 -d "$1" aggregate >.ic-log 2>&1; then
            grep -qE "error: 30[1278]" .ic-log && exit 4
            exit 2
        fi
        head="$(git -C "$1" rev-parse HEAD)"
        [ -z "$2" ] || [ "$head" = "$2" ] || exit 3
        rm -rf "$1/.git"
        echo "$head" > .ic-head
    ' sh "$alias" "$expect" >/dev/null 2>&1 || rc=$?
    IC_GIT_ENV=()
    # The credential's HOME goes as soon as the container has.
    rm -rf "${home:?}"
    if [ "$rc" -eq 0 ]; then
        # Written by the container: a plain file naming a commit, or the
        # download failed (10-nov-2026).
        if [ -f "$tmp/.ic-head" ] && [ ! -L "$tmp/.ic-head" ]; then
            IC_FETCHED="$(head -c 64 "$tmp/.ic-head" 2>/dev/null || true)"
        fi
        if [[ ! "$IC_FETCHED" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] \
            || [ ! -d "$tmp/$alias" ] || [ -L "$tmp/$alias" ]; then
            IC_FETCHED=""
            rc=2
        fi
    fi
    if [ "$rc" -ne 0 ]; then
        # git-aggregator's last lines, whatever failed but the statuses
        # that say why themselves (M6): 125 is Docker's, 128 git's.
        case "$rc" in
            3|4|5|124|137) ;;
            *)
                if [ -f "$tmp/.ic-log" ] && [ ! -L "$tmp/.ic-log" ]; then
                    tail -n 15 "$tmp/.ic-log" | grep -vi 'authorization' >&2 || true
                fi
                ;;
        esac
        rm -rf "${tmp:?}"
        return "$rc"
    fi
    mkdir "$tmp/entry"
    mv "$tmp/$alias" "$tmp/entry/tree"
    IC_ENTRY_TMP="$tmp/entry"
}

# Move the downloaded entry <new> into the host's cache as <key>, with
# <marker> as its marker, and print where the cached tree is. Another job
# that put the same tree there first wins: <new> is then dropped.
#
# Usage: ic_tree_store <new> <key> <marker>
ic_tree_store() {
    local new="$1" entry="$IC_TREES/$2"
    printf '%s\n' "$3" > "$new/head"
    if ic_tree_ready "$entry" "$3"; then
        rm -rf "${new:?}"
    elif [ -e "$entry" ] || [ -L "$entry" ]; then
        # An entry cut short or not the cache's: replaced.
        rm -rf "${entry:?}"
        mv -T "$new" "$entry"
    elif ! mv -T "$new" "$entry" 2>/dev/null; then
        rm -rf "${new:?}"
    fi
    printf '%s' "$entry"
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
        # same host (a cron may enqueue N builds in one tick).
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
        # does not have.
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
        # already present. Callers that only ship prod.yaml (a layer's own
        # builds) pass it explicitly.
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
        # An instance back from fetching its repositories on the host
        # (the series named in incubacloud.repos_in_image_series): its
        # trees, their markers and the build's ignore file go, or the
        # image's gitaggregate would find directories without ``.git``
        # where it clones, and fail.
        ic_unfetch "$dir"
        if [ -n "$GIT_HELPER_IMAGE" ]; then
            ic_git_helper_prepare "$GIT_HELPER_IMAGE" \
                || ic_die "could not prepare the git helper image"
            GIT_HELPER_IMAGE="$(ic_git_helper_tag "$GIT_HELPER_IMAGE")"
        fi
        ic_resolve_heads "$spec" "$heads"
        # Rewritten by every build: kept out of the instance's history.
        ic_git_exclude "$dir" odoo/custom/incubacloud-heads.txt
        ;;

    git-trust)
        ic_require_args 1 "$#" "deploy.sh git-trust <dir> <spec_file>"
        spec="$1"
        # What the image's git needs to clone from a Git provider other
        # than GitHub (a series incubacloud.repos_in_image_series sends
        # back to cloning inside the build): no
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
        ic_require_args 3 "$#" \
            "deploy.sh fetch-repos <dir> <spec_file> <helper_image> <series> [retry_wait] [budget]"
        spec="$1"
        GIT_HELPER_IMAGE="$2"
        SERIES="$3"
        retry_wait="${4:-60}"
        budget="${5:-$FETCH_BUDGET}"
        [[ "$SERIES" =~ ^[0-9]{1,2}\.[0-9]$ ]] || ic_die "invalid series: $SERIES"
        # Every repository of the spec is downloaded here, by the helper
        # container, and the image builds with AGGREGATE=false: no
        # credential enters the image, its build or the instance's
        # container, and no ``.git`` does either (10-nov-2026; 8.0 to
        # 10.0 since 8-nov-2026, whose git 2.1.4 sends a token in the
        # clear after a redirect). Each tree is the host's cached one at
        # its commits, linked into odoo/custom/src/<alias> (see
        # IC_TREES). See ic_resolve_heads for the spec, one line per
        # merge; ``pin`` lines carry their URL too. The step keeps its
        # budget by itself (SECONDS, from its start).
        SECONDS=0
        src="$dir/odoo/custom/src"
        [ -d "$src" ] || ic_die "build context not found: $src"
        [ -f "$src/repos.yaml" ] || ic_die "repos.yaml not found in $src"
        if [ -L "$IC_TREES" ] || [ -L "$(dirname "$IC_TREES")" ]; then
            ic_die "the host's cache of trees is a link: $IC_TREES"
        fi
        IC_GIT_ALL_IN_CONTAINER=1
        # The spec holds the credentials and serves this step alone: it
        # goes when the step ends, however it ends.
        IC_TMP_DIRS+=("$spec")
        ic_git_traps
        # 1. Before fetching anything: what a run cut short left, and the
        #    exclusions from the instance's history, so that a job killed
        #    between moving a tree and excluding it never lets
        #    ``rebuild.sh commit-dirty`` commit the trees. The containers
        #    first: one still running would write into a temporary tree
        #    removed under it, or read its token from a HOME removed.
        ic_git_reap
        ic_fetch_lock "$src"
        ic_git_sweep
        (umask 077 && mkdir -p "$IC_TREES")
        [ ! -L "$IC_TREES/.lock" ] || rm -f "$IC_TREES/.lock"
        exec 8>>"$IC_TREES/.lock"
        IC_ACCESS_HOME=""
        for leftover in "$src"/.ic-tmp-* "$IC_TREES/.tmp-$IC_JOB_TOKEN-"*; do
            [ -e "$leftover" ] || [ -L "$leftover" ] || continue
            rm -rf "${leftover:?}"
        done
        # The markers' directory is never read or written through a
        # link, and the temporary markers of a killed ic_marker_link,
        # which the removal of dropped trees does not see, go too.
        heads_dir="$(ic_heads_dir "$src")"
        ic_heads_dir_unlink "$src"
        if [ -d "$heads_dir" ]; then
            rm -f "$heads_dir"/.tmp-*
        fi
        # What cloning in the image needed, which nothing builds with now.
        rm -f "$dir/odoo/custom/build.d/060-incubacloud-git"
        rm -rf "${dir:?}/odoo/custom/incubacloud-git"
        declare -A IDX_OF=()
        ROWS=()
        ALIASES=()
        while IFS= read -r line; do
            ic_split_tabs "$line"
            alias="${IC_COLS[0]}"
            [ -n "$alias" ] || continue
            if [[ ! "$alias" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] \
                || ic_reserved_alias "$alias"; then
                ic_die "invalid repository alias: $alias"
            fi
            if [ -z "${IDX_OF[$alias]+x}" ]; then
                ALIASES+=("$alias")
                IDX_OF[$alias]="${#ROWS[@]}"
            else
                IDX_OF[$alias]+=" ${#ROWS[@]}"
            fi
            ROWS+=("$line")
        done < "$spec"
        for alias in "${ALIASES[@]}"; do
            ic_git_exclude "$dir" "odoo/custom/src/$alias/"
        done
        ic_git_exclude "$dir" 'odoo/custom/src/.ic-*'
        ic_git_exclude "$dir" odoo/Dockerfile.dockerignore
        ic_git_exclude "$dir" odoo/custom/incubacloud-heads.txt
        # 2. The helper image, under its local tag the daily prune spares.
        ic_git_helper_prepare "$GIT_HELPER_IMAGE" \
            || ic_die "could not prepare the git helper image"
        GIT_HELPER_IMAGE="$(ic_git_helper_tag "$GIT_HELPER_IMAGE")"
        # 3. The heads, as record-heads reads them: one per merge.
        heads="$dir/odoo/custom/incubacloud-heads.txt"
        ic_resolve_heads "$spec" "$heads"
        HEAD_AT=()
        while read -r _alias _ref sha; do
            HEAD_AT+=("$sha")
        done < "$heads"
        [ "${#HEAD_AT[@]}" -eq "${#ROWS[@]}" ] \
            || ic_die "the heads read do not match the spec"
        # 4. Each repository whose tree is not at its heads already: from
        #    the host's cache when it has it, else downloaded into it.
        fetched=0
        cached=0
        for alias in "${ALIASES[@]}"; do
            read -ra idx <<< "${IDX_OF[$alias]}"
            marker=""
            key_lines=""
            unknown=""
            for i in "${idx[@]}"; do
                ic_split_tabs "${ROWS[$i]}"
                [[ "${HEAD_AT[$i]}" != unknown-* ]] || unknown=1
                if [ "${#idx[@]}" -eq 1 ]; then
                    marker="${HEAD_AT[$i]}"
                    key_lines="$(ic_strip_userinfo "${IC_COLS[3]}") ${HEAD_AT[$i]}"
                else
                    marker+="${IC_COLS[2]} ${HEAD_AT[$i]}"$'\n'
                    key_lines+="$(ic_strip_userinfo "${IC_COLS[3]}") ${IC_COLS[2]} ${HEAD_AT[$i]}"$'\n'
                fi
            done
            if [ "${#idx[@]}" -gt 1 ]; then
                marker="$(printf '%s' "$marker" | sha256sum | cut -c1-64)"
            fi
            if [ -z "$unknown" ] && [ -d "$src/$alias" ] \
                && [ ! -L "$src/$alias" ] \
                && [ "$(ic_marker_read "$src" "$alias")" = "$marker" ]; then
                ic_log "$alias is at $marker already"
                continue
            fi
            entry=""
            if [ -z "$unknown" ]; then
                entry="$IC_TREES/$(ic_tree_key "$key_lines")"
                if ic_tree_ready "$entry" "$marker" \
                    && ! ic_pins_readable "$alias"; then
                    ic_warn "$alias: its pinned commit could not be asked with its credential: it is downloaded, not taken from the host's cache"
                else
                    ic_trees_lock
                    if ic_tree_ready "$entry" "$marker"; then
                        ic_tree_link "$src" "$alias" "$entry"
                        ic_trees_unlock
                        ic_log "$alias at $marker, from the host's cache"
                        cached=$((cached + 1))
                        continue
                    fi
                    ic_trees_unlock
                fi
            fi
            attempt=1
            while :; do
                left=$((budget - SECONDS))
                [ "$left" -gt 0 ] \
                    || ic_die "the step's budget of ${budget}s ran out before $alias was fetched"
                secs="$FETCH_TIMEOUT"
                [ "$left" -ge "$secs" ] || secs="$left"
                rc=0
                ic_fetch_tree "$src" "$alias" "$secs" || rc=$?
                [ "$rc" -ne 0 ] || break
                case "$rc" in
                    3) ic_die "$alias: the commit that arrived is not the one pinned" ;;
                    4) ic_die "$alias: the server redirects; update the repository's URL" ;;
                    5) ic_die "$alias: its URL is refused: credentials go only to the server they are for, over https, and a URL off GitHub must name that server over https" ;;
                    124|137)
                        if [ "$secs" -lt "$FETCH_TIMEOUT" ]; then
                            ic_die "the step's budget of ${budget}s ran out while $alias was fetched"
                        fi
                        ic_die "$alias: the download did not end within ${secs}s"
                        ;;
                esac
                if [ "$attempt" -ge 2 ]; then
                    ic_die "$alias: the download failed twice (status $rc)"
                fi
                if [ $((budget - SECONDS - retry_wait)) -le 0 ]; then
                    ic_die "$alias: the download failed (status $rc), and the step's budget of ${budget}s leaves no time to try again"
                fi
                ic_warn "$alias: the download failed (status $rc); trying again in ${retry_wait}s"
                # Waited for, so a termination reaches the trap at once,
                # and without the lock, which a sleep left behind would
                # keep.
                sleep "$retry_wait" 9>&- 8>&- &
                wait "$!"
                attempt=$((attempt + 1))
            done
            got="$IC_FETCHED"
            if [ "${#idx[@]}" -eq 1 ]; then
                # The head may have moved since it was read: the commit
                # that arrived is the one named, in the marker and in the
                # heads file.
                i="${idx[0]}"
                ic_split_tabs "${ROWS[$i]}"
                marker="$got"
                key_lines="$(ic_strip_userinfo "${IC_COLS[3]}") $got"
                if [ "${HEAD_AT[$i]}" != "$got" ]; then
                    awk -v n="$((i + 1))" -v g="$got" \
                        'NR == n {$3 = g} {print}' "$heads" > "$heads.ic-tmp"
                    mv "$heads.ic-tmp" "$heads"
                fi
            elif ! ic_heads_unmoved "$alias"; then
                # Which merge brought what is not known: the tree is
                # this instance's alone, under a name no other asks for,
                # and its marker matches no heads, so the next run
                # takes it from the cache or downloads it again (M12).
                ic_warn "$alias: a head of its merges moved during the download, or could not be read again: its tree is not shared, and the next run fetches it again"
                marker="moved-$(date +%s%N)-$$-$RANDOM"
                key_lines+="$marker"$'\n'
            fi
            ic_trees_lock
            entry="$(ic_tree_store "$IC_ENTRY_TMP" "$(ic_tree_key "$key_lines")" "$marker")"
            ic_tree_link "$src" "$alias" "$entry"
            ic_trees_unlock
            ic_log "$alias fetched at $got"
            fetched=$((fetched + 1))
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
            if ic_reserved_alias "$name" || [ -n "${IDX_OF[$name]+x}" ]; then
                continue
            fi
            rm -f "${marker:?}"
            if [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]] \
                && [ -d "$src/$name" ] && [ ! -L "$src/$name" ]; then
                rm -rf "${src:?}/${name:?}"
                ic_log "removed the tree of $name, no longer a repository of the instance"
            fi
        done
        # 5. The build context: the template's .dockerignore leaves
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
            for alias in "${ALIASES[@]}"; do
                printf '!custom/src/%s\n' "$alias"
            done
            printf 'custom/src/.ic-*\n'
        } > "$ignore.ic-tmp"
        mv "$ignore.ic-tmp" "$ignore"
        # 6. The seconds this took, which the build's repetition counts.
        printf '%s\n' "$SECONDS" > "$src/.ic-fetch-seconds"
        ic_log "${#ALIASES[@]} repositories ready for the build, $fetched fetched, $cached from the host's cache, in ${SECONDS}s"
        ;;

    *)
        ic_die "unknown operation: $op"
        ;;
esac
