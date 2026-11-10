#!/usr/bin/env bats
# Tests for scripts/read_pg_activity.sh.
#
# The script runs a Python program inside the instance's odoo container
# and the panel parses the one JSON line it prints
# (``cloud.instance._parse_db_activity``). ``docker`` is stubbed to run
# that program here, with a stand-in ``psycopg2`` answering the queries
# as PostgreSQL 16 would: the totals, who blocks whom and the database's
# name, which travels in the environment and never in the program.

setup() {
    SCRIPT="${BATS_TEST_DIRNAME}/../../scripts/read_pg_activity.sh"
    TMP="$(mktemp -d)"
    DIR="$TMP/inst"
    mkdir -p "$DIR" "$TMP/bin" "$TMP/py"
    cat > "$TMP/bin/docker" <<'STUB'
#!/usr/bin/env bash
# docker compose exec -T -e IC_DB=<db> odoo sh -c <program>
printf '%s\n' "$*" >> "$DOCKER_CALLS"
[ -z "${DOCKER_FAILS:-}" ] || { echo "service \"odoo\" is not running" >&2; exit 1; }
for a in "$@"; do
    case "$a" in IC_DB=*) export "$a" ;; esac
done
exec sh -c "${*: -1}"
STUB
    chmod +x "$TMP/bin/docker"
    cat > "$TMP/py/psycopg2.py" <<'PY'
"""A stand-in for psycopg2: canned answers to the script's queries."""
import os

SESSIONS = [
    (791, "odoo", "odoo-12", "idle in transaction", "Client", "ClientRead",
     [], 400, 400, "SELECT 1"),
    (812, "odoo", "odoo-13", "active", "Lock", "transactionid",
     [791], 372, 372, "UPDATE stock_quant SET quantity = 1"),
    (900, "odoo", "odoo-14", "idle", "Client", "ClientRead",
     [], 5, None, "COMMIT"),
]
SESSION_COLUMNS = ("pid", "user", "application", "state", "wait_event_type",
                   "wait_event", "blocked_by", "age", "xact_age", "query")


class Cursor:
    description = None

    def execute(self, sql):
        self.description = None
        if "server_version_num" in sql:
            self.rows = [(160004,)]
        elif "pg_stat_activity" in sql:
            self.description = [(name,) for name in SESSION_COLUMNS]
            self.rows = SESSIONS
        elif sql.startswith("SHOW max_connections"):
            self.rows = [("100",)]
        elif "pg_stat_database" in sql:
            self.rows = [(990, 10, 2, 7340032)]
        elif "pg_stat_user_tables" in sql:
            self.description = [(n,) for n in ("table", "live", "dead", "last_vacuum")]
            self.rows = [("stock_quant", 10, 4, None)]
        else:
            raise AssertionError("unexpected query: " + sql)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class Connection:
    autocommit = False

    def cursor(self):
        return Cursor()

    def close(self):
        pass


def connect(dbname):
    with open(os.environ["IC_DBNAME_SEEN"], "w") as seen:
        seen.write(dbname)
    return Connection()
PY
    export PATH="$TMP/bin:$PATH"
    export PYTHONPATH="$TMP/py"
    export DOCKER_CALLS="$TMP/calls"
    export IC_DBNAME_SEEN="$TMP/dbname"
    : > "$DOCKER_CALLS"
}

teardown() {
    rm -rf "$TMP"
}

# Read one field of the JSON line the script printed.
field() {
    python3 -c 'import json, sys; d = json.loads(sys.argv[1]); print(eval(sys.argv[2], {}, {"d": d}))' "$output" "$1"
}

@test "one JSON line with the totals and who blocks whom" {
    run bash "$SCRIPT" "$DIR" prod
    [ "$status" -eq 0 ]
    [ "${#lines[@]}" -eq 1 ]
    [ "$(field 'd["summary"]["connections"]')" = 3 ]
    [ "$(field 'd["summary"]["max_connections"]')" = 100 ]
    [ "$(field 'd["summary"]["active"]')" = 1 ]
    [ "$(field 'd["summary"]["waiting_lock"]')" = 1 ]
    [ "$(field 'd["summary"]["idle_in_transaction"]')" = 1 ]
    [ "$(field 'd["summary"]["longest_active"]')" = 372 ]
    [ "$(field 'd["summary"]["longest_idle_in_transaction"]')" = 400 ]
    [ "$(field 'd["summary"]["cache_hit_ratio"]')" = 99.0 ]
    [ "$(field 'd["summary"]["deadlocks"]')" = 2 ]
    # 791 holds the lock 812 waits on.
    [ "$(field 'd["sessions"][0]["blocks"]')" = "[812]" ]
    [ "$(field 'd["sessions"][1]["blocked_by"]')" = "[791]" ]
    [ "$(field 'd["tables"][0]["table"]')" = stock_quant ]
    grep -q -- "compose exec -T -e IC_DB=prod odoo sh -c" "$DOCKER_CALLS"
}

@test "the database's name reaches the program through its environment" {
    run bash "$SCRIPT" "$DIR" "it's; drop"
    [ "$status" -eq 0 ]
    [ "$(cat "$IC_DBNAME_SEEN")" = "it's; drop" ]
}

@test "a container that is not running fails the reading" {
    DOCKER_FAILS=1 run bash "$SCRIPT" "$DIR" prod
    [ "$status" -ne 0 ]
    [[ "$output" == *"is not running"* ]]
}

@test "a missing directory and wrong arguments are refused" {
    run bash "$SCRIPT" "$TMP/nowhere" prod
    [ "$status" -eq 1 ]
    run bash "$SCRIPT" "$DIR"
    [ "$status" -eq 2 ]
    [ ! -s "$DOCKER_CALLS" ]
}
