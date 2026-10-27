#!/usr/bin/env bash
# Shared helpers sourced by every incubacloud remote operation script.
#
# Entry scripts live next to this file under ``scripts/`` and are
# uploaded to the host by ``AbstractExecutor.run_script()``, which
# invokes them as ``bash <dir>/<name>.sh <args...>``. Source this
# library with:
#
#     source "$(dirname "$0")/lib/common.sh"
#
# It deliberately does NOT enable strict mode on behalf of its caller:
# every entry script declares its own ``set -euo pipefail`` so that
# choice stays visible where the script is read.
#
# The ERROR prefix is not cosmetic: the job log classifier renders any
# line matching \b(ERROR|FATAL|CRITICAL)\b as a red [err] entry, so
# ic_die surfaces correctly in the panel without extra plumbing.

# Print a progress line to the job log.
ic_log() {
    printf '[incubacloud] %s\n' "$*"
}

# Print a non-fatal warning to the job log.
ic_warn() {
    printf '[incubacloud] WARNING: %s\n' "$*" >&2
}

# Print an error and abort the script with a non-zero exit status.
ic_die() {
    printf '[incubacloud] ERROR: %s\n' "$*" >&2
    exit 1
}

# Expand a leading ``~/`` in a path.
#
# Instance directories are stored as ``~/project/instance`` (see
# cloud.instance.get_remote_dir) and reach a script as a quoted
# argument, so the shell never expands the tilde for us. Expanding it
# here — on the host, against the real $HOME — is also more correct than
# guessing the remote home from the panel.
ic_expand_home() {
    # SC2088 fires on the literal tilde in these patterns, which is
    # precisely the point: we are matching an *unexpanded* tilde that
    # arrived as data, not writing one we expect the shell to expand.
    # shellcheck disable=SC2088
    case "$1" in
        "~") printf '%s\n' "$HOME" ;;
        "~/"*) printf '%s\n' "${HOME}${1#\~}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# Keep a path of the copier project out of its git repository's index.
#
# The project directory is a git repository, and a rebuild commits
# whatever it holds before ``copier update`` (rebuild.sh commit-dirty).
# Two kinds of path do not belong in it:
#
# * What the instance's stack writes on its own: Odoo its live log in
#   ``logs/`` (instance_logs.sh), continuous recovery's pitr agent its
#   spool in ``.walg/`` (incubacloud_pitr), a mark per uploaded segment,
#   their removal an hour later and each filestore copy. Tracked, the
#   working tree goes dirty again between ``git add -A`` and the moment
#   ``copier update`` looks — and copier refuses to run on a dirty tree,
#   failing the whole rebuild after every other step has already
#   succeeded. Measured in production for the log: only the instance
#   with live traffic failed; its idle sibling, rebuilt in the same
#   second, did not. In QA for the spool (run 7016, 2026-10-27): a plan
#   change, whose failure holds the instance's queue.
# * Secrets: committed, they stay in the history for good, past any
#   rotation of the file.
#
# Written to ``.git/info/exclude`` and not ``.gitignore`` because copier
# owns ``.gitignore``: editing it would provoke the very template
# conflict this exists to avoid.
#
# Nothing here is fatal. Failing to exclude leaves the path in the next
# commit, which is worse than a rebuild but not worse than aborting one.
#
# Usage: ic_git_exclude <dir> <path>
#   <dir>   the copier project directory
#   <path>  relative to it, as a .gitignore line anchored at its top: a
#           directory ends in ``/`` (``logs/``), a file or a pattern
#           does not (``odoo/custom/conf.d/*.conf``)
ic_git_exclude() {
    local dir="$1" path="$2" exclude
    [ -d "$dir/.git" ] || return 0
    exclude="$dir/.git/info/exclude"
    mkdir -p "$dir/.git/info" 2>/dev/null || true
    if ! grep -qxF "/$path" "$exclude" 2>/dev/null; then
        # A file not ending in a newline would glue the pattern onto
        # whatever the last line happens to be.
        if [ -s "$exclude" ] && [ -n "$(tail -c 1 "$exclude")" ]; then
            printf '\n' >> "$exclude" 2>/dev/null || true
        fi
        printf '/%s\n' "$path" >> "$exclude" 2>/dev/null \
            || ic_warn "could not write $exclude: $path stays tracked"
    fi
    # Untrack what earlier rebuilds already committed. ``--cached``
    # leaves every file on disk: this only takes them out of the index.
    # ``-f`` because git refuses a path staged with content that neither
    # HEAD nor the disk has (a commit that failed after staging, then a
    # file rewritten), and ``--cached`` makes forcing it lose nothing.
    if git -C "$dir" ls-files --error-unmatch "$path" >/dev/null 2>&1; then
        git -C "$dir" rm -r --cached -f --quiet "$path" \
            || ic_warn "could not untrack $path in $dir: the next commit still takes it"
    fi
}

# Keep the instance's live Odoo log out of the copier repo's index
# (see ic_git_exclude).
#
# Usage: ic_git_exclude_logs <dir>
ic_git_exclude_logs() {
    ic_git_exclude "$1" logs/
}

# Abort unless a script received the arguments it needs.
# Usage: ic_require_args <needed> "$#" "<usage line>"
ic_require_args() {
    local needed="$1" given="$2" usage="${3-}"
    if [ "$given" -lt "$needed" ]; then
        ic_die "expected at least ${needed} argument(s), got ${given}.${usage:+ Usage: ${usage}}"
    fi
}

# Print the service list of the compose in the current directory,
# retrying once. Returns 1 — with docker's own error in the job log —
# when the command fails both times.
#
# ``docker compose config`` failing and it answering a list without a
# given service are different facts with different fixes, and a caller
# that pipes the command straight into ``grep`` cannot tell them apart:
# a failed read looks exactly like an empty list. Production,
# 2026-09-19: two deletes fired on one host in the same millisecond,
# one of them saw the command die in under 110 ms, and the purge
# reported "this host's compose declares no backup service" — a
# permanent-drift diagnosis, with "rebuild the instance" as its advice —
# for something the same command answered correctly 11 minutes later.
# The stderr that would have said why was going to /dev/null.
#
# So the caller only ever sees a real list. The retry is here and not in
# the executor because the read has no side effects: a second attempt
# costs two seconds on the host, not a second job.
#
# The line logged on retry is deliberate: a transient that a retry
# absorbs is otherwise invisible, and one that keeps happening should
# not be.
ic_compose_services() {
    local err out attempt
    err="$(mktemp)"
    for attempt in 1 2; do
        if out="$(docker compose config --services 2>"$err")"; then
            rm -f "$err"
            printf '%s\n' "$out"
            return 0
        fi
        if [ "$attempt" -eq 1 ]; then
            # stderr on purpose: stdout is this function's return value
            # and callers capture it, so a line printed there would be
            # swallowed into the service list instead of reaching the log.
            ic_log "docker compose config failed; retrying once in 2s" >&2
            sleep 2
        fi
    done
    ic_warn "could not read the compose in $PWD; docker compose config said:"
    sed 's/^/[docker] /' "$err" >&2
    rm -f "$err"
    return 1
}

# ── One-off containers ───────────────────────────────────────────────────
#
# A ``docker compose run`` container inherits its service's labels, and
# what reads those labels takes it for the live service. Traefik routes
# visitors to a container that serves nothing. Sablier counts it in a
# sleeping tenant's group: while it lives, visitors get the waiting page
# instead of their panel (QA, 2026-10-22: an ``odoo shell`` push of the
# tenant's settings, in the very seconds the customer first opened the
# panel), and an expired session or a Sablier restart stops it
# mid-work. Sablier has no setting to leave one-offs out; it only reads
# ``sablier.enable``.
#
# So every one-off a script starts goes through ic_compose_run, and every
# "what is running" a script asks goes through ic_running_services. A
# test (tests/test_oneoff_containers.py) fails on the raw commands.

# The labels that keep a one-off out of Traefik and Sablier; the
# executors' COMPOSE_RUN (models/abstract_executor.py) carries the same.
IC_ONEOFF_LABELS=(--label traefik.enable=false --label sablier.enable=false)

# ``docker compose run`` for the compose in the current directory, out of
# Traefik and Sablier. Takes exactly the arguments ``docker compose run``
# takes.
ic_compose_run() {
    docker compose run "${IC_ONEOFF_LABELS[@]}" "$@"
}

# Print the services of the compose in the current directory that are
# running now, one per line, one-off containers left out. Returns 1 when
# docker cannot list them, so a caller can tell "nothing runs" from
# "could not read".
#
# ``docker compose ps --services --status running`` counts a one-off as
# its service from compose 2.29 on (the hosts run 5.x), and so do
# ``ps --format`` and ``ps -q --status running``; 2.20 did not. The
# ``com.docker.compose.oneoff`` label is what every version agrees on.
#
# A container gone between the listing and the inspect (a one-off that
# just ended) makes ``docker inspect`` fail for that id alone; it is not
# running, so that is not a failed read.
ic_running_services() {
    local ids
    ids="$(docker compose ps -q --status running)" || return 1
    [ -n "$ids" ] || return 0
    # Word splitting is the point: one container id per word.
    # shellcheck disable=SC2086
    { docker inspect -f '{{if ne (index .Config.Labels "com.docker.compose.oneoff") "True"}}{{index .Config.Labels "com.docker.compose.service"}}{{end}}' $ids 2>/dev/null || true; } \
        | awk 'NF' | sort -u
}

# ── The instance's database tool ─────────────────────────────────────────
#
# What dumps and loads an instance's database inside its ``odoo``
# container depends on the instance's Odoo series, and the panel knows
# it (``models/_odoo_versions.py``): the executor names it in
# ``IC_DB_TOOL``, and the two functions below turn an operation into the
# argv of that tool, in ``IC_DB_ARGS``, so a script reads the same
# whichever tool is in use:
#
#     ic_db_dump_args "$dbname" /host-tmp/out.zip all
#     ic_compose_run --rm odoo "${IC_DB_ARGS[@]}"
#
# ``click-odoo`` (click-odoo-contrib, every series up to 19) and
# ``odoo-db`` (``odoo db``, Odoo 18 and later, the only one on 20). Unset
# means click-odoo, so a script run by hand behaves as it always did. A
# series that needs a third tool is one more case here and a value in
# its card, nothing else.

# The argv the two functions below build, read by the script that
# called them ("${IC_DB_ARGS[@]}").
# shellcheck disable=SC2034  # set here, read by the calling script
IC_DB_ARGS=()

# Print the tool in use, refusing one this library does not implement.
ic_db_tool() {
    local tool="${IC_DB_TOOL:-click-odoo}"
    case "$tool" in
        click-odoo|odoo-db) printf '%s\n' "$tool" ;;
        *) ic_die "unknown database tool: $tool (expected click-odoo or odoo-db)" ;;
    esac
}

# Fill IC_DB_ARGS with the argv that dumps <db> into <archive>.
#   ic_db_dump_args <db> <archive> <all|db>
# ``all`` carries the filestore, ``db`` the database alone. Both tools
# write Odoo's own zip (dump.sql, manifest.json, and filestore/ for
# ``all``), so a download is the same archive whichever tool made it.
# shellcheck disable=SC2034  # IC_DB_ARGS is read by the calling script
ic_db_dump_args() {
    local database="$1" archive="$2" scope="$3" tool
    tool="$(ic_db_tool)" || exit 1
    case "$scope" in
        all|db) ;;
        *) ic_die "ic_db_dump_args: unknown scope: $scope (expected all or db)" ;;
    esac
    case "$tool" in
        click-odoo)
            if [ "$scope" = all ]; then
                IC_DB_ARGS=(click-odoo-backupdb --filestore "$database" "$archive")
            else
                IC_DB_ARGS=(click-odoo-backupdb --no-filestore "$database" "$archive")
            fi
            ;;
        odoo-db)
            if [ "$scope" = all ]; then
                IC_DB_ARGS=(odoo db dump "$database" "$archive")
            else
                IC_DB_ARGS=(odoo db dump --no-filestore "$database" "$archive")
            fi
            ;;
    esac
}

# Fill IC_DB_ARGS with the argv that loads <archive> into <db>, replacing
# a database of that name, as a copy (a new database uuid, so the copy
# never answers for the original) and neutralized when <neutralize> is 1.
#   ic_db_load_args <db> <archive> <0|1>
# shellcheck disable=SC2034  # IC_DB_ARGS is read by the calling script
ic_db_load_args() {
    local database="$1" archive="$2" neutralize="${3:-0}" tool
    tool="$(ic_db_tool)" || exit 1
    local flags=()
    [ "$neutralize" = 1 ] && flags+=(--neutralize)
    case "$tool" in
        click-odoo)
            IC_DB_ARGS=(click-odoo-restoredb "${flags[@]}" --copy --force "$database" "$archive")
            ;;
        odoo-db)
            # ``odoo db load`` copies unless told --move.
            IC_DB_ARGS=(odoo db load "${flags[@]}" --force "$database" "$archive")
            ;;
    esac
}
