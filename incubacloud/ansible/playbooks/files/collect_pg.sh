#!/usr/bin/env bash
# Each instance's PostgreSQL gauges, for node_exporter's textfile collector.
#
# Usage: collect_pg.sh <instances file> <textfile dir>
#
#   instances file  one line per instance:
#                   "<instance_id> <instance_name> <compose project> <dbname> <dbuser>"
#   textfile dir    where node_exporter reads ``*.prom`` (the agents' own)
#
# Installed by ``host_observability.yml`` as root, with its instances file,
# under /usr/local/lib/incubacloud/, and run every minute from
# /etc/cron.d/incubacloud-collect-pg. Root because it reads each database
# through the socket of its ``db`` container as the container's own
# filesystem shows it, ``/proc/<pid>/root/run/postgresql``, with the host's
# psql: 83 ms of CPU per instance against 169 ms for a ``docker exec`` of
# the container's psql (measured with 10 instances, F0 of the observability
# plan, 9-nov-2026). The images' ``local all all trust`` lets the socket in
# without a password, so no credential is written anywhere.
#
# One query per instance, in a read-only session that gives up after 3 s
# to connect and 3 s to answer; a whole pass stops asking after 45 s, so
# twenty instances that hang still end within the minute, and a pass that
# finds the previous one still running exits at once. A ``db`` container
# that is stopped (a sleeping instance) or paused is left out without
# counting as an error; one that runs and does not answer is an error.
#
# Writes <textfile dir>/ic_pg.prom in one rename: node_exporter never reads
# half a file. Exits 0 without writing when the directory is gone (the
# agents were removed) or the instances file is.
set -uo pipefail
export LC_ALL=C

LIST="${1:?usage: collect_pg.sh <instances file> <textfile dir>}"
OUT_DIR="${2:?usage: collect_pg.sh <instances file> <textfile dir>}"
# In /run, which only root writes: a lock in a directory anybody writes is
# a file somebody else may have put there first.
LOCK="${IC_PG_LOCK:-/run/incubacloud-collect-pg.lock}"
PROC="${IC_PG_PROC:-/proc}"
BUDGET="${IC_PG_BUDGET:-45}"
PER_INSTANCE="${IC_PG_TIMEOUT:-6}"

export PGCONNECT_TIMEOUT=3
export PGOPTIONS='-c statement_timeout=3000 -c default_transaction_read_only=on'
export PGAPPNAME=incubacloud-collect-pg

[ -d "$OUT_DIR" ] && [ ! -L "$OUT_DIR" ] || exit 0
[ -r "$LIST" ] || exit 0
exec 9>"$LOCK" || exit 1
flock -n 9 || exit 0

deadline=$((SECONDS + BUDGET))
started="${EPOCHREALTIME:-$(date +%s)}"

read -r -d '' QUERY <<'SQL'
SELECT
  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()),
  current_setting('max_connections')::int,
  (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend' AND state = 'active' AND pid <> pg_backend_pid()),
  (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'),
  (SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%'),
  COALESCE((SELECT extract(epoch FROM max(now() - query_start))::bigint FROM pg_stat_activity
            WHERE backend_type = 'client backend' AND state = 'active' AND pid <> pg_backend_pid()), 0),
  COALESCE((SELECT extract(epoch FROM max(now() - state_change))::bigint FROM pg_stat_activity
            WHERE state LIKE 'idle in transaction%'), 0),
  d.deadlocks,
  d.temp_bytes,
  CASE WHEN d.blks_hit + d.blks_read > 0
       THEN round(d.blks_hit::numeric / (d.blks_hit + d.blks_read), 6) ELSE 1 END
FROM pg_stat_database d WHERE d.datname = current_database()
SQL

# The gauges, in the query's order, with their type and help.
NAMES=(ic_pg_connections ic_pg_max_connections ic_pg_active ic_pg_waiting_lock
       ic_pg_idle_in_transaction ic_pg_longest_active_seconds
       ic_pg_longest_idle_in_tx_seconds ic_pg_deadlocks_total
       ic_pg_temp_bytes_total ic_pg_cache_hit_ratio)
TYPES=(gauge gauge gauge gauge gauge gauge gauge counter counter gauge)
HELPS=("Client connections to the instance's PostgreSQL."
       "PostgreSQL's max_connections."
       "Client connections running a query."
       "Sessions waiting for a lock."
       "Sessions idle inside an open transaction."
       "Age of the oldest running query, in seconds."
       "Age of the oldest idle open transaction, in seconds."
       "Deadlocks detected in the instance's database."
       "Bytes of temporary files the database wrote."
       "Share of block reads served from PostgreSQL's cache.")

# esc VALUE: VALUE as a label value of the text format.
esc() {
    local s="${1//\\/\\\\}"
    printf '%s' "${s//\"/\\\"}"
}

# The PID of every running ``db`` container, by compose project. Two
# docker calls for the whole pass, whatever the number of instances.
declare -A PID=()
mapfile -t ids < <(timeout 5 docker ps -q --filter label=com.docker.compose.service=db 2>/dev/null)
if [ "${#ids[@]}" -gt 0 ]; then
    while read -r project pid running paused; do
        if [ "$running" = true ] && [ "$paused" = false ] && [ "${pid:-0}" != 0 ]; then
            PID[$project]=$pid
        fi
    done < <(timeout 5 docker inspect -f \
        '{{index .Config.Labels "com.docker.compose.project"}} {{.State.Pid}} {{.State.Running}} {{.State.Paused}}' \
        "${ids[@]}" 2>/dev/null)
fi

samples=()
errors=0
skipped=0
while read -r iid iname project db user; do
    [ -n "${iid:-}" ] || continue
    case "$iid" in \#*) continue ;; esac
    pid="${PID[$project]:-}"
    # Stopped, sleeping or paused: nothing runs to be asked.
    [ -n "$pid" ] || continue
    left=$((deadline - SECONDS))
    if [ "$left" -le 0 ]; then
        skipped=$((skipped + 1))
        continue
    fi
    sock="$PROC/$pid/root/run/postgresql"
    [ -d "$sock" ] || sock="$PROC/$pid/root/var/run/postgresql"
    wait=$((left < PER_INSTANCE ? left : PER_INSTANCE))
    if ! row="$(timeout "$wait" psql -X -w -q -A -t -F ' ' -h "$sock" \
        -U "$user" -d "$db" -c "$QUERY" 2>/dev/null)" \
        || ! [[ "$row" =~ ^-?[0-9.]+(\ -?[0-9.]+){9}$ ]]; then
        errors=$((errors + 1))
        continue
    fi
    read -r -a vals <<<"$row"
    labels="instance_id=\"$(esc "$iid")\",instance_name=\"$(esc "$iname")\""
    for k in "${!NAMES[@]}"; do
        samples[k]+="${NAMES[$k]}{${labels}} ${vals[$k]}"$'\n'
    done
done <"$LIST"

ended="${EPOCHREALTIME:-$(date +%s)}"
tmp="$(mktemp "$OUT_DIR/.ic_pg.prom.XXXXXX")" || exit 1
trap 'rm -f "$tmp"' EXIT
# Everything through the descriptor mktemp's file was opened on: the
# directory is not root's, and a name in it may change under this script.
exec 3>"$tmp" || exit 1
{
    for k in "${!NAMES[@]}"; do
        printf '# HELP %s %s\n# TYPE %s %s\n' "${NAMES[$k]}" "${HELPS[$k]}" "${NAMES[$k]}" "${TYPES[$k]}"
        printf '%s' "${samples[k]:-}"
    done
    printf '# HELP ic_pg_scrape_errors Running databases that did not answer in this pass.\n'
    printf '# TYPE ic_pg_scrape_errors gauge\nic_pg_scrape_errors %s\n' "$errors"
    printf '# HELP ic_pg_scrape_skipped Running databases left unasked when the pass ran out of time.\n'
    printf '# TYPE ic_pg_scrape_skipped gauge\nic_pg_scrape_skipped %s\n' "$skipped"
    printf '# HELP ic_pg_scrape_duration_seconds How long the pass took.\n'
    printf '# TYPE ic_pg_scrape_duration_seconds gauge\n'
    awk -v a="$started" -v b="$ended" 'BEGIN { printf "ic_pg_scrape_duration_seconds %.3f\n", b - a }'
} >&3
chmod 0644 "/proc/self/fd/3" 2>/dev/null || chmod 0644 "$tmp"
exec 3>&-
mv -f "$tmp" "$OUT_DIR/ic_pg.prom"
trap - EXIT
