"""Cursors that wait for a row instead of losing a race over it.

Odoo opens every cursor at REPEATABLE READ (see the docstring in
``odoo/sql_db.py``), which is the right default for request handling:
a whole request reads one consistent snapshot. It is the wrong default
for a background writer that only stamps a column, because under
snapshot isolation two transactions writing the *same row* cannot both
win — the second is aborted with ``could not serialize access due to
concurrent update`` even when the columns they touch are disjoint.

That is exactly the shape of the instance telemetry: the SSH health
probe stamps ``last_health_check`` on ``cloud_instance`` while the
metrics cron stamps ``metrics_last_seen`` on the same rows, on their own
schedules. They collided every couple of days. The loser did not simply
retry quietly either — PostgreSQL's error reached Odoo's own SQL layer,
which logs ``bad query`` at ERROR before anything can catch it, and the
instance log scraper then reported those lines back to the panel as an
``instance_error_logs`` alert. A savepoint-and-retry would have fixed
the failure and kept the log noise, so the fix has to be to not lose
the race in the first place.

READ COMMITTED does that: the second writer blocks on the row lock,
then re-reads and applies. No abort, no ERROR line, no alert.
"""
import logging
from contextlib import contextmanager

from odoo.sql_db import Cursor

_logger = logging.getLogger(__name__)


def try_advisory_xact_lock(cr, namespace, scope=""):
    """Try a transaction-scoped advisory lock isolated by database.

    PostgreSQL advisory locks are cluster-global, not database-local.  The
    hashed key therefore includes the current database OID, a caller-owned
    namespace and an optional scope such as a user ID.  The non-blocking call
    lets HTTP workers reject contention instead of waiting for another long
    operation to finish.

    :param cr: active Odoo cursor whose transaction will own the lock
    :param str namespace: stable feature-specific lock namespace
    :param scope: stable resource identifier within the namespace
    :return: whether this transaction acquired the lock
    :rtype: bool
    """
    cr.execute(
        """
        SELECT pg_try_advisory_xact_lock(
            hashtextextended(
                (SELECT oid::text
                   FROM pg_database
                  WHERE datname = current_database())
                || ':' || %s || ':' || %s,
                0
            )
        )
        """,
        (str(namespace), str(scope)),
    )
    return bool(cr.fetchone()[0])


@contextmanager
def read_committed_cursor(registry):
    """Yield a fresh cursor running at READ COMMITTED.

    ``SET TRANSACTION`` is only legal before the transaction's first
    query, which is why this opens the cursor itself rather than taking
    one: by the time a caller could hand us a cursor, it has usually
    already read something.

    Which is also why the isolation level is only set on a *real*
    cursor. ``registry.cursor()`` does not always return one — under
    tests it hands back a pseudo-cursor riding on an outer transaction
    that has already run queries, and asking that to change isolation
    is both illegal and meaningless: the transaction is not ours to
    re-declare. Yield it untouched and let it inherit whatever the
    owner chose.

    :param registry: the Odoo registry to open the cursor on
    """
    with registry.cursor() as cr:
        if isinstance(cr, Cursor):
            cr.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        yield cr


def run_after_commit(cr, label, func):
    """Run *func* once *cr* commits; log, never raise, what it raises.

    ``cr.postcommit`` runs its callbacks in order after the commit, and
    one that raises stops the rest and reaches whoever committed: for a
    job, queue_job's runner, which then stores the job failed although it
    has just been stored done. So every callback is wrapped here, and
    what it raises is logged with *label* and its traceback, the only
    trace left of it. A rollback drops the callbacks, which is the point:
    what the transaction did not record is not announced.

    *func* opens what cursor it needs itself (``registry.cursor()``):
    *cr* is committed by then, and its snapshot is not one to act on.

    :param cr: the cursor whose commit *func* waits for
    :param str label: what *func* does, for the log
    :param func: a function of no arguments
    """

    def _guarded():
        """Call *func*; log what it raises."""
        try:
            func()
        except Exception:
            _logger.exception("after the commit: %s failed", label)

    cr.postcommit.add(_guarded)
