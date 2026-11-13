#!/usr/bin/env bash
# The steps of a move between hosts that read or stop the instance.
#
# Usage:
#   move_steps.sh quiet       <dir> <pg_user> <dbname> <sql>
#   move_steps.sh fingerprint <dir> <pg_user> <dbname> <table>...
#   move_steps.sh verify      <dir> <pg_user> <dbname> <files> <table>=<rows>...
#   move_steps.sh freeze      <dir>
#
# ``quiet`` runs <sql>, one statement answering one integer, in the
# instance's database and prints that integer: what is busy inside it
# right before the move would stop it (MoveQuietCheckExecutor). The SQL
# arrives as one quoted argument and is piped into psql's stdin, so the
# remote shell never parses it. A stack whose ``db`` is not running is
# parked, so nothing runs in it: it prints 0 without starting anything.
#
# ``fingerprint`` prints one line of JSON on the source of a move, once
# its Odoo has stopped: the rows of each <table> that exists, and the
# files of the database's filestore,
#     {"tables": {"res_users": 3, ...}, "files": 12}
# A table the database does not have is left out. ``db`` is started
# when the stack is parked: the dump that follows needs it too.
#
# ``verify`` counts the same on the destination, between the restore of
# the database and the start of Odoo, and compares with what the source
# counted (<files>, and <table>=<rows> for each table it had). A
# difference prints "Integrity check failed: <what> source N, target M"
# for each and exits 1, so the restore stops before Odoo starts there;
# none prints "✓ Integrity verified: K tables, N files."
#
# ``freeze`` stops every container of the stack and keeps it — volumes,
# directory, database — as the old copy an operator can still restore
# from by hand. Each container's restart policy is set to ``no`` first:
# with ``always``, a reboot of the host would start the old copy again,
# its crons and its mail with it, beside the instance that moved. Then
# the networks of the stack's compose project go (``release_networks``):
# each holds one of the host's address pools for as long as it exists,
# about 31 without ``default-address-pools``, and the stopped copy needs
# none. Its containers still point at them, so the copy cannot be
# started as it is; ``docker compose down`` (never ``-v``) and a ``run``
# or an ``up`` make it again from the same images, volumes and directory.
#
# Every table name is checked against [a-z_][a-z0-9_]* before it
# reaches SQL.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 2 "$#" "move_steps.sh <operation> <dir> ..."

op="$1"
dir="$(ic_expand_home "$2")"
shift 2

[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

# Refuse anything but a plain table name.
check_table() {
    [[ "$1" =~ ^[a-z_][a-z0-9_]*$ ]] || ic_die "not a table name: $1"
}

# Print "<table> <rows>" for each existing table among the arguments.
# Usage: count_rows <pg_user> <dbname> <table>...
count_rows() {
    local pg_user="$1" dbname="$2" list="" table
    shift 2
    for table in "$@"; do
        check_table "$table"
        list+="${list:+,}'$table'"
    done
    [ -n "$list" ] || return 0
    # One psql for every table: the count runs inside the cut.
    docker compose exec -T db psql -U "$pg_user" -d "$dbname" -tA -F ' ' \
        -v ON_ERROR_STOP=1 -c \
"SELECT t, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', t), false, true, '')))[1]::text \
FROM unnest(ARRAY[$list]) AS t WHERE to_regclass(format('public.%I', t)) IS NOT NULL ORDER BY t"
}

# Print how many files the database's filestore holds.
# Usage: count_files <dbname>
count_files() {
    # A one-off without doodba's entrypoint answers at once; without
    # ``--no-deps`` compose would recreate a ``db`` whose definition
    # changed since it started. The container's shell expands its $1.
    # shellcheck disable=SC2016
    ic_compose_run --rm --no-deps --entrypoint="" odoo sh -c \
        'find "/var/lib/odoo/filestore/$1" -type f 2>/dev/null | wc -l' \
        sh "$1" | tr -d '\r\n '
}

# Remove the networks the compose project of the given containers made.
# Only its own: an external network (the proxy's) carries no project
# label and stays. A network that will not go is left, with a warning,
# for the removal of the copy (``docker compose down``), and fails
# nothing: the copy is stopped by then, which is what matters.
# Usage: release_networks <container id>...
release_networks() {
    local project nets net
    [ "$#" -gt 0 ] || return 0
    project="$(docker inspect -f \
        '{{index .Config.Labels "com.docker.compose.project"}}' "$1")" \
        || { ic_warn "could not read the compose project of $dir; its networks stay"; return 0; }
    [ -n "$project" ] || return 0
    nets="$(docker network ls -q \
        --filter "label=com.docker.compose.project=$project")" \
        || { ic_warn "could not list the networks of $project; they stay"; return 0; }
    [ -n "$nets" ] || return 0
    ic_log "releasing the networks of $project"
    for net in $nets; do
        docker network rm "$net" >/dev/null \
            || ic_warn "could not remove network $net of $project; it goes with the copy"
    done
}

# Start ``db`` when it is not running and wait until it answers.
ensure_db() {
    local running
    running="$(ic_running_services)" \
        || ic_die "could not read which services are running in $dir"
    grep -qx db <<<"$running" && return 0
    ic_log "the stack is parked; starting db"
    docker compose start db >/dev/null || ic_die "could not start db"
    timeout 60 sh -c \
        'until docker compose exec -T db pg_isready -q; do sleep 1; done' \
        || ic_die "db did not accept connections within 60 s"
}

case "$op" in
    quiet)
        ic_require_args 3 "$#" "move_steps.sh quiet <dir> <pg_user> <dbname> <sql>"
        pg_user="$1"; dbname="$2"; sql="$3"
        running="$(ic_running_services)" \
            || ic_die "could not read which services are running in $dir"
        if ! grep -qx db <<<"$running"; then
            ic_log "the stack is parked: nothing runs in it"
            echo 0
            exit 0
        fi
        printf '%s\n' "$sql" \
            | docker compose exec -T db psql -U "$pg_user" -d "$dbname" \
                -tA -v ON_ERROR_STOP=1 \
            | tr -d '\r ' | awk 'NF' | tail -n 1
        ;;
    fingerprint)
        ic_require_args 2 "$#" \
            "move_steps.sh fingerprint <dir> <pg_user> <dbname> <table>..."
        pg_user="$1"; dbname="$2"
        shift 2
        ensure_db
        rows="$(count_rows "$pg_user" "$dbname" "$@")"
        files="$(count_files "$dbname")"
        [[ "$files" =~ ^[0-9]+$ ]] || ic_die "could not count the filestore's files"
        json=""
        while read -r table count; do
            [ -n "$table" ] || continue
            json+="${json:+, }\"$table\": $count"
        done <<<"$rows"
        echo "{\"tables\": {$json}, \"files\": $files}"
        ;;
    verify)
        ic_require_args 3 "$#" \
            "move_steps.sh verify <dir> <pg_user> <dbname> <files> <table>=<rows>..."
        pg_user="$1"; dbname="$2"; source_files="$3"
        shift 3
        [[ "$source_files" =~ ^[0-9]+$ ]] || ic_die "not a file count: $source_files"
        declare -A expected=()
        tables=()
        for pair in "$@"; do
            table="${pair%%=*}"
            count="${pair#*=}"
            check_table "$table"
            [[ "$count" =~ ^[0-9]+$ ]] || ic_die "not a row count: $pair"
            expected["$table"]="$count"
            tables+=("$table")
        done
        declare -A found=()
        if [ "${#tables[@]}" -gt 0 ]; then
            while read -r table count; do
                [ -n "$table" ] || continue
                found["$table"]="$count"
            done <<<"$(count_rows "$pg_user" "$dbname" "${tables[@]}")"
        fi
        files="$(count_files "$dbname")"
        [[ "$files" =~ ^[0-9]+$ ]] || ic_die "could not count the filestore's files"
        failed=0
        for table in "${tables[@]}"; do
            target="${found[$table]:-missing}"
            if [ "$target" != "${expected[$table]}" ]; then
                echo "Integrity check failed: $table source ${expected[$table]}, target $target"
                failed=1
            fi
        done
        if [ "$files" != "$source_files" ]; then
            echo "Integrity check failed: filestore files source $source_files, target $files"
            failed=1
        fi
        [ "$failed" -eq 0 ] || exit 1
        echo "✓ Integrity verified: ${#tables[@]} tables, $files files."
        ;;
    freeze)
        ids="$(docker compose ps -aq)" \
            || ic_die "could not list the containers of $dir"
        if [ -n "$ids" ]; then
            # Word splitting is the point: one container id per word.
            # shellcheck disable=SC2086
            docker update --restart=no $ids >/dev/null
        fi
        ic_log "stopping every container in $dir and keeping them"
        docker compose stop
        # Word splitting again: the ids, then the first one's project.
        # shellcheck disable=SC2086
        release_networks $ids
        ;;
    *)
        ic_die "unknown operation: $op"
        ;;
esac
