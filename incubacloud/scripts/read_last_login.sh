#!/usr/bin/env bash
# Read the most recent login recorded inside an instance's own database.
#
# Usage: read_last_login.sh <instance_dir> <dbname> <dbuser> <dbpassword>
#
# Prints exactly one line:
#
#   LAST_LOGIN <ISO timestamp>   somebody authenticated, most recently then
#   LAST_LOGIN none              the table is there and nobody ever logged in
#
# and exits non-zero with an ic_die message when the reading could not be
# taken at all. That distinction is the whole point of this script: "no
# reading" and "nobody logged in" must never collapse into each other,
# because the caller deletes instances on the strength of the second one.
# A stopped container, an unreachable database or a missing table are all
# failures, never evidence of disuse.
#
# Which table holds the answer is decided by looking at the database, not
# by what the panel believes the instance's Odoo version to be. Odoo 9
# and later keep one row per session in ``res_users_log``; 7 and 8 have
# only ``res_users.login_date``. The panel's ``odoo_version`` is operator
# data and can be wrong or empty, while the catalogue cannot.
#
# The platform manages Odoo 7.0 through 19.0, so neither the Python nor
# the SQL may assume a modern stack: the interpreter is whichever of
# python3/python the image has, the program is valid under both 2 and 3,
# and the table check uses ``information_schema`` rather than
# ``to_regclass`` (PostgreSQL 9.4+).
#
# psycopg2 is used rather than psql because the Odoo container is
# guaranteed to have it and is not guaranteed to have a postgres client,
# and because parameters stay parameters: no credential is ever spliced
# into a shell word. The program travels on stdin for the same reason —
# nothing of it passes through a quoting layer.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 4 "$#" \
    "read_last_login.sh <instance_dir> <dbname> <dbuser> <dbpassword>"

dir="$(ic_expand_home "$1")"
db="$2"
db_user="$3"
db_password="$4"

[ -d "$dir" ] || ic_die "instance directory not found: $dir"

cd "$dir"

# The credentials travel in the environment of the container process,
# not on its command line, so they stay out of the host's process list.
docker compose exec -T \
    -e IC_DB="$db" -e IC_USER="$db_user" -e IC_PASSWORD="$db_password" \
    odoo sh -c '
if command -v python3 >/dev/null 2>&1; then
    exec python3 -
else
    exec python -
fi
' <<'PYEOF' || ic_die "could not read the last login from the instance database"
import os
import psycopg2

conn = psycopg2.connect(
    host="db",
    dbname=os.environ["IC_DB"],
    user=os.environ["IC_USER"],
    password=os.environ["IC_PASSWORD"],
)
cur = conn.cursor()
cur.execute(
    "SELECT 1 FROM information_schema.tables"
    " WHERE table_schema = 'public' AND table_name = 'res_users_log'"
)
if cur.fetchone():
    cur.execute("SELECT max(create_date) FROM res_users_log")
else:
    cur.execute("SELECT max(login_date) FROM res_users")
stamp = cur.fetchone()[0]
conn.close()
print("LAST_LOGIN " + (stamp.isoformat() if stamp else "none"))
PYEOF
