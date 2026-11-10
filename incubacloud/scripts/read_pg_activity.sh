#!/usr/bin/env bash
# Print what an instance's database is doing right now, as one JSON line.
#
# Usage: read_pg_activity.sh <instance_dir> <dbname>
#
# The sessions of the instance's database (who, which program, state,
# what they wait on, for how long, which sessions block them and the
# first 300 characters of their query), the totals the panel's
# "Queries now" line shows (connections and their ceiling, active,
# waiting on a lock, idle in a transaction, the oldest of each, cache
# hits, deadlocks, size) and the ten tables with most dead rows with
# their last vacuum. Read when somebody asks, never stored: the text of
# a query can carry personal data.
#
# The query runs with psycopg2 inside the instance's odoo container, as
# ``read_last_login.sh`` does: the container is guaranteed to have it and
# not a postgres client, and the platform manages Odoo 7.0 to 19.0, so
# the program is valid under Python 2 and 3. It connects with the
# container's own libpq environment (``PGHOST``, ``PGUSER``,
# ``PGPASSWORD``, which the Doodba template gives the odoo service), so no
# credential crosses SSH or a command line; only the database's name is
# passed. A PostgreSQL older than 9.6 has no wait events nor
# ``pg_blocking_pids``: those fields come back empty.
#
# The panel sends this program on stdin when somebody asks, rather than
# uploading it as a job does, so it sources nothing.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: read_pg_activity.sh <instance_dir> <dbname>" >&2
    exit 2
fi
dir="$1"
db="$2"
[ -d "$dir" ] || { echo "instance directory not found: $dir" >&2; exit 1; }
cd "$dir"

# shellcheck disable=SC2016  # the inner sh expands nothing of ours
docker compose exec -T -e IC_DB="$db" odoo sh -c '
if python3 -c "import psycopg2" 2>/dev/null; then
    exec python3 -
else
    exec python -
fi
' <<'PYEOF'
import json
import os

import psycopg2

QUERY_CHARS = 300
SESSIONS = 50

conn = psycopg2.connect(dbname=os.environ["IC_DB"])
conn.autocommit = True
cur = conn.cursor()


def rows(sql):
    cur.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


cur.execute("SELECT current_setting('server_version_num')::int")
version = cur.fetchone()[0]
modern = version >= 96000
sessions = rows(
    "SELECT pid, usename AS user, application_name AS application, state,"
    + (" wait_event_type, wait_event, pg_blocking_pids(pid) AS blocked_by,"
       if modern else
       " NULL AS wait_event_type, NULL AS wait_event, NULL AS blocked_by,")
    + " extract(epoch FROM now() - CASE WHEN state = 'active'"
      " THEN query_start ELSE state_change END)::int AS age,"
      " extract(epoch FROM now() - xact_start)::int AS xact_age,"
      " left(query, %d) AS query"
      " FROM pg_stat_activity"
      " WHERE datname = current_database() AND pid <> pg_backend_pid()"
      " ORDER BY xact_start NULLS LAST, query_start NULLS LAST" % QUERY_CHARS
)
cur.execute("SHOW max_connections")
max_connections = int(cur.fetchone()[0])
cur.execute(
    "SELECT blks_hit, blks_read, deadlocks, pg_database_size(datname)"
    " FROM pg_stat_database WHERE datname = current_database()"
)
hit, read, deadlocks, size = cur.fetchone()
tables = rows(
    "SELECT relname AS table, n_live_tup AS live, n_dead_tup AS dead,"
    " to_char(greatest(last_autovacuum, last_vacuum),"
    " 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS last_vacuum"
    " FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10"
)
conn.close()

blocks = {}
for s in sessions:
    for pid in s["blocked_by"] or ():
        blocks.setdefault(pid, []).append(s["pid"])
for s in sessions:
    s["blocked_by"] = list(s["blocked_by"] or ())
    s["blocks"] = blocks.get(s["pid"], [])
active = [s for s in sessions if s["state"] == "active"]
idle_tx = [s for s in sessions if (s["state"] or "").startswith("idle in transaction")]
waiting = [s for s in sessions if s["wait_event_type"] == "Lock"]
print(json.dumps({
    "summary": {
        "connections": len(sessions),
        "max_connections": max_connections,
        "active": len(active),
        "waiting_lock": len(waiting),
        "idle_in_transaction": len(idle_tx),
        "longest_active": max([s["age"] or 0 for s in active] or [0]),
        "longest_idle_in_transaction": max(
            [s["xact_age"] or 0 for s in idle_tx] or [0]),
        "cache_hit_ratio": (
            round(100.0 * hit / (hit + read), 2) if (hit + read) else None),
        "deadlocks": deadlocks,
        "size_bytes": size,
        "server_version": version,
    },
    "sessions": sessions[:SESSIONS],
    "tables": tables,
}, default=str))
PYEOF
