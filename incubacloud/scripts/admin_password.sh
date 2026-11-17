#!/usr/bin/env bash
# Give an instance's Odoo administrator a random password, if it still
# has Odoo's default one.
#
# Usage: admin_password.sh <instance_dir> <dbname> <stopped|running> [<since>]
#
# ``stopped`` is for a database whose Odoo does not run (a deploy before
# its start, a restore before Odoo starts on what it loaded): the program
# runs in a one-off container of the odoo service. ``running`` is for an
# instance that serves: it runs in the odoo container, and when the
# password changed Odoo is restarted, so that the sessions opened with the
# old one, and what Odoo keeps in memory of the uid and password an RPC
# call signed in with (``res.users._check_uid_passwd``), end with it.
#
# <since>, seconds since the epoch (0 when omitted), is when the panel
# last heard an answer from this database: see the mark below.
#
# The administrator is the user ``base.user_admin`` names (12.0 on),
# else ``base.user_root`` (the administrator up to 11.0), read in the
# database and never decided by the series the panel believes the
# instance runs. Its password is changed only when what is stored still
# verifies ``admin`` in a column that series reads: from 12.0 ``password``
# alone (a ``password_crypt`` a migrated database kept is not read any
# more); before, ``password`` and, with ``auth_crypt`` installed,
# ``password_crypt``. A password someone set is left as it is. The new
# one is drawn here, stored as Odoo stores it (a hash in ``password`` from
# 12.0; before, a hash in ``password_crypt`` with ``auth_crypt``
# installed, else the password itself in ``password``) and forgotten: it
# is never printed, logged nor handed back, and neither is the hash.
# Whoever needs to sign in with a password of their own signs in with
# Connect and sets it in Odoo.
#
# The change is committed with a mark in the same transaction, the
# system parameter ``incubacloud.admin_password_replaced_at``: the second
# since the epoch it was made, and nothing else (no password, no hash).
# A run that finds a password of its own and a mark newer than <since>
# answers ``changed`` all the same: the change was this panel's, and its
# answer was lost on the way (a connection cut after the commit), so it
# is told now and, ``running``, Odoo restarted.
#
# Prints exactly one line, and exits 0:
#
#   ADMIN_PASSWORD changed   it had the default one, it has a random one
#                            (``running``: and Odoo was restarted)
#   ADMIN_PASSWORD own       it has a password of its own, left as it is
#   ADMIN_PASSWORD absent    the database has no such user
#
# Any other outcome exits non-zero, among them a database that still
# accepts ``admin`` once the change is written (the stored password is read
# again after the commit) and, ``running``, an Odoo that could not be
# restarted after the change: no answer is printed then, and the next run
# answers ``changed`` from the mark and restarts it.
#
# Like read_last_login.sh, for Odoo 7.0 to 20.0: psycopg2 and passlib, both
# in every image Odoo runs in, a program valid under Python 2 and 3 that
# travels on stdin, and the database reached the way Odoo reaches it in
# that container (``PGHOST``, ``PGUSER`` and ``PGPASSWORD`` of its
# environment): no credential passes through an argument.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib/common.sh
source "$(dirname "$0")/lib/common.sh"

ic_require_args 3 "$#" \
    "admin_password.sh <instance_dir> <dbname> <stopped|running> [<since>]"

dir="$(ic_expand_home "$1")"
db="$2"
mode="$3"
since="${4:-0}"
here="$(cd "$(dirname "$0")" && pwd)"

case "$mode" in
    stopped | running) ;;
    *) ic_die "unknown mode: $mode" ;;
esac
[[ "$since" =~ ^[0-9]+$ ]] || ic_die "not a number of seconds: $since"
[ -d "$dir" ] || ic_die "instance directory not found: $dir"
cd "$dir"

# The interpreter is the first of python3/python the image has that
# imports both libraries: the images of 8.0 to 10.0 have no python3, and
# one that had it next to Odoo's python 2 would lack them. It runs in the
# container, whose shell expands it.
# shellcheck disable=SC2016
pick_python='
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 \
            && "$py" -c "import psycopg2, passlib" >/dev/null 2>&1; then
        exec "$py" -
    fi
done
echo "no python with psycopg2 and passlib in this image" >&2
exit 2
'

# The program, on stdin.
program() {
    cat <<'PYEOF'
import base64
import os
import sys
import time

import psycopg2

try:
    from passlib.context import CryptContext
except ImportError:
    sys.stderr.write("passlib is not installed in this image\n")
    sys.exit(2)

# What Odoo verifies, in every series: pbkdf2_sha512 (auth_crypt from 8.0,
# base from 12.0), md5_crypt (early auth_crypt) and, last, a password
# stored as it is.
CTX = CryptContext(
    schemes=["pbkdf2_sha512", "md5_crypt", "plaintext"],
    deprecated=["md5_crypt", "plaintext"],
)
DEFAULT = "admin"
#: The system parameter that marks a change made by this program.
MARK = "incubacloud.admin_password_replaced_at"


def connect():
    """Open the database, waiting up to two minutes for it to take it."""
    deadline = time.time() + 120
    while True:
        try:
            return psycopg2.connect(dbname=os.environ["IC_DB"])
        except psycopg2.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(2)


def is_default(value):
    """Whether a stored password is Odoo's default one."""
    if not value:
        return False
    try:
        return bool(CTX.verify(DEFAULT, value))
    except (TypeError, ValueError):
        return False


def one(cur, query, params=()):
    """The first row of *query*, or None."""
    cur.execute(query, params)
    return cur.fetchone()


def mark(cur):
    """The second the mark says the password was replaced, or 0."""
    row = one(cur, "SELECT value FROM ir_config_parameter WHERE key = %s", (MARK,))
    try:
        return int((row and row[0]) or 0)
    except ValueError:
        return 0


def set_mark(cur):
    """Write the mark, now, in the transaction of the change."""
    now = str(int(time.time()))
    cur.execute("UPDATE ir_config_parameter SET value = %s WHERE key = %s", (now, MARK))
    if not cur.rowcount:
        cur.execute(
            "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)",
            (MARK, now),
        )


def admin_id(cur):
    """The administrator's id: base.user_admin, else base.user_root."""
    for name in ("user_admin", "user_root"):
        row = one(
            cur,
            "SELECT res_id FROM ir_model_data"
            " WHERE module = 'base' AND name = %s AND model = 'res.users'",
            (name,),
        )
        if row:
            return row[0]
    return None


def series(cur):
    """The major version of base installed in the database, or None."""
    row = one(cur, "SELECT latest_version FROM ir_module_module WHERE name = 'base'")
    try:
        return int(((row and row[0]) or "").split(".")[0])
    except ValueError:
        return None


def has_auth_crypt(cur):
    """Whether auth_crypt is installed."""
    return bool(one(
        cur,
        "SELECT 1 FROM ir_module_module"
        " WHERE name = 'auth_crypt' AND state = 'installed'",
    ))


def read_columns(present, major, crypt):
    """The columns of res_users Odoo of *major* checks a password against.

    From 12.0 ``password`` alone: a ``password_crypt`` left by a database
    migrated from 11.0 or older is not read. Before 12.0, ``password``
    (compared as it is stored) and, with ``auth_crypt``, ``password_crypt``.
    An unknown series: every column there is.
    """
    if major is None:
        return list(present)
    if major >= 12 or not crypt:
        return [c for c in present if c == "password"]
    return list(present)


def stored(cur, uid, columns):
    """The administrator's stored passwords by column, locked, or None."""
    row = one(
        cur,
        "SELECT " + ", ".join(columns) + " FROM res_users WHERE id = %s FOR UPDATE",
        (uid,),
    )
    return dict(zip(columns, row)) if row else None


def new_values(columns, major, crypt):
    """The columns to write for a new random password, as Odoo stores it."""
    secret = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii")
    hasher = getattr(CTX, "hash", None) or CTX.encrypt
    hashed = hasher(secret)
    if major is not None and major >= 12:
        return {"password": hashed}
    if crypt and "password_crypt" in columns:
        return {"password": "", "password_crypt": hashed}
    if major is not None:
        # Up to 11.0 without auth_crypt (7.0 always, 8.0 to 11.0 when it
        # is not installed) Odoo compares the password with what
        # ``password`` holds, as it is: the random password is stored in
        # clear there, because that is the only way Odoo accepts it.
        values = {"password": secret}
        if "password_crypt" in columns:
            values["password_crypt"] = hashed
        return values
    return {"password": hashed}


def main():
    since = int(os.environ.get("IC_SINCE") or 0)
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_schema = current_schema() AND table_name = 'res_users'"
    )
    present = set(row[0] for row in cur.fetchall())
    columns = [c for c in ("password", "password_crypt") if c in present]
    major = series(cur)
    crypt = "password_crypt" in columns and has_auth_crypt(cur)
    read = read_columns(columns, major, crypt)
    uid = admin_id(cur) if read else None
    values = stored(cur, uid, read) if uid is not None else None
    if values is None:
        conn.rollback()
        print("ADMIN_PASSWORD absent")
        return 0
    if not any(is_default(v) for v in values.values()):
        changed_before = mark(cur) > since
        conn.rollback()
        print("ADMIN_PASSWORD changed" if changed_before else "ADMIN_PASSWORD own")
        return 0
    update = new_values(columns, major, crypt)
    names = sorted(update)
    cur.execute(
        "UPDATE res_users SET " + ", ".join(n + " = %s" for n in names)
        + " WHERE id = %s",
        [update[n] for n in names] + [uid],
    )
    set_mark(cur)
    conn.commit()
    after = stored(cur, uid, read)
    conn.rollback()
    conn.close()
    if after is None or any(is_default(v) for v in after.values()):
        sys.stderr.write("the administrator still accepts the default password\n")
        return 3
    print("ADMIN_PASSWORD changed")
    return 0


sys.exit(main())
PYEOF
}

if [ "$mode" = stopped ]; then
    out="$(program | ic_compose_run --rm -T -e IC_DB="$db" -e IC_SINCE="$since" odoo sh -c "$pick_python")" \
        || ic_die "could not check the administrator's password"
else
    out="$(program | docker compose exec -T -e IC_DB="$db" -e IC_SINCE="$since" odoo sh -c "$pick_python")" \
        || ic_die "could not check the administrator's password"
fi

answer="$(grep -oE '^ADMIN_PASSWORD (changed|own|absent)$' <<<"$out" | tail -n 1 || true)"
[ -n "$answer" ] || ic_die "the check of the administrator's password gave no answer"

case "$answer" in
    *changed)
        ic_log "the administrator had Odoo's default password: it has a random one now"
        if [ "$mode" = running ]; then
            # Sessions opened with the old password end with it, and so
            # does what Odoo keeps in memory of the RPC calls signed in
            # with it. 24: restarted, the steps after it not checked; 27:
            # restarted on the previous build a failed rebuild left marked.
            # 26 restarted nothing.
            rc=0
            bash "$here/compose_op.sh" "$dir" restart odoo || rc=$?
            if [ "$rc" -ne 0 ] && [ "$rc" -ne 24 ] && [ "$rc" -ne 27 ]; then
                ic_die "the password was replaced, but Odoo could not be restarted (exit $rc): what it keeps in memory of the old one lasts until it is"
            fi
        fi
        ;;
    *own) ic_log "the administrator has a password of its own: left as it is" ;;
    *absent) ic_log "the database has no administrator user" ;;
esac
echo "$answer"
