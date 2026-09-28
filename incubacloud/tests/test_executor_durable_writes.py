"""What an executor writes has to outlive the job's transaction.

``cloud.job.execute`` ends with ``env.clear()``, which drops every write
the job's transaction never flushed, and queue_job rolls that transaction
back when the job fails. Core 1.0.135 minted the metrics deletion key
there: VictoriaMetrics started with it and the database never received
it. The test that let it through read the value back through the ORM in
the very environment that wrote it, where a pending write looks exactly
like a saved one.

So these tests end the job the way the runner does and read the row by
SQL. ``JobEndAssertions`` is the reusable half; the executor tests of the
other suites use it for their own durable writes.

One thing a ``TransactionCase`` cannot reproduce: in test mode the
durable cursor is a pseudo-cursor on the test's own transaction, so the
job's REPEATABLE READ snapshot never hides anything. That part was
measured against a real database with two cursors (plan F0, 28-sep);
here it is pinned as behaviour — the job reads the new value from its
cache, without a query.
"""
from odoo.tests.common import TransactionCase
from odoo.tools import SQL

from odoo.addons.incubacloud.models.abstract_executor import (
    DURABLE_LOCK_TIMEOUT,
    AbstractExecutor,
)
from odoo.addons.incubacloud.models.password_utils import decrypt_value


class _Probe(AbstractExecutor):
    """The smallest concrete executor: no commands, no hooks."""

    _job_type = None

    def get_commands(self):
        """Run nothing."""
        return []


class JobEndAssertions:
    """Ask what survives the end of a job, the way the runner ends it.

    Mixed into a ``TransactionCase``; relies on its ``self.env``.
    """

    def end_job(self):
        """Drop the job transaction's pending writes, as the runner does."""
        self.env.clear()

    def stored(self, record, fname):
        """Return *fname* of *record* as the database holds it, read by SQL.

        Encrypted values come back decrypted so tests compare plain text;
        anything else is returned as the column holds it.

        :param record: a single record
        :param str fname: a stored column of its model
        """
        self.env.cr.execute(SQL(
            "SELECT %s FROM %s WHERE id = %s",
            SQL.identifier(fname), SQL.identifier(record._table), record.id,
        ))
        value = self.env.cr.fetchone()[0]
        return decrypt_value(value, self.env) if isinstance(value, str) else value


def make_probe_job(env, name="durable"):
    """Return ``(host, job)`` records a probe executor can be built on.

    :param env: the test environment
    :param str name: suffix keeping the host name unique per test
    """
    host = env["cloud.host"].create({
        "name": f"{name}-host",
        "ip_address": "192.0.2.77",
        "user": "ubuntu",
        "wildcard_domain": f"{name}.example.com",
    })
    job_type = env["cloud.job.type"].search(
        [("code", "=", "host_probe")], limit=1,
    ) or env["cloud.job.type"].create({
        "name": "host_probe", "code": "host_probe", "apply_to": "host",
    })
    job = env["cloud.job"].create({
        "name": f"{name} probe",
        "host_id": host.id,
        "job_type_id": job_type.id,
    })
    return host, job


class DurableCase(JobEndAssertions, TransactionCase):

    def setUp(self):
        """A probe executor on a real job, and settings in a known state."""
        super().setUp()
        self.registry_enter_test_mode()
        self.host, self.job = make_probe_job(self.env)
        self.executor = _Probe(self.job, self.host)
        self.settings = self.env["cloud.settings"].sudo()._get_system()
        self.settings.write({
            "metrics_account": "acct_before",
            "metrics_retention_days": 30,
            "metrics_central_host_id": False,
            "metrics_delete_auth_key": "key-before",
        })
        project = self.env["cloud.project"].create({"name": "Durable"})
        self.inst = self.env["cloud.instance"].create({
            "name": "durableinst", "project_id": project.id,
            "environment": "staging", "pip_dependencies": "requests==2",
        })
        # The durable cursor reads the database, not this env's cache.
        self.env.flush_all()


class TestDurableEnv(DurableCase):

    def test_commits_on_a_clean_exit(self):
        """What the block writes is saved once it exits."""
        with self.executor._durable_env() as env:
            env["cloud.settings"].browse(self.settings.id).write(
                {"metrics_retention_days": 31},
            )
        self.end_job()
        self.assertEqual(self.stored(self.settings, "metrics_retention_days"), 31)

    def test_rolls_back_when_the_block_raises(self):
        """A block that raises saves nothing."""
        with self.assertRaises(RuntimeError), self.executor._durable_env() as env:
            env["cloud.settings"].browse(self.settings.id).write(
                {"metrics_retention_days": 31},
            )
            raise RuntimeError("boom")
        self.end_job()
        self.assertEqual(self.stored(self.settings, "metrics_retention_days"), 30)

    def test_keeps_the_jobs_identity(self):
        """
        Same user, superuser flag and context as the job, on another cursor.
        """
        with self.executor._durable_env() as env:
            self.assertEqual(env.uid, self.job.env.uid)
            self.assertEqual(env.su, self.job.env.su)
            self.assertEqual(env.context, self.job.env.context)
            self.assertIsNot(env.cr, self.job.env.cr)

    def test_leaves_a_borrowed_transaction_alone(self):
        """The pseudo-cursor rides on the test's transaction: not ours to set."""
        self.env.cr.execute("SHOW lock_timeout")
        before = self.env.cr.fetchone()[0]
        with self.executor._durable_env() as env:
            env.cr.execute("SHOW lock_timeout")
            self.assertEqual(env.cr.fetchone()[0], before)


class TestDurableEnvOnARealCursor(TransactionCase):
    """Outside test mode ``registry.cursor()`` is a real connection."""

    def test_caps_the_lock_wait_and_reads_committed(self):
        """A real cursor gets the lock timeout and READ COMMITTED."""
        host, job = make_probe_job(self.env, "real")
        executor = _Probe(job, host)
        # Nothing is written: the records above exist only in this test's
        # uncommitted transaction, which the real cursor cannot see.
        with executor._durable_env() as env:
            env.cr.execute("SHOW lock_timeout")
            self.assertEqual(env.cr.fetchone()[0], DURABLE_LOCK_TIMEOUT)
            env.cr.execute("SHOW transaction_isolation")
            self.assertEqual(env.cr.fetchone()[0], "read committed")


class TestPersist(DurableCase):

    def _new_values(self):
        """Return one new settings value per field type ``_persist`` carries."""
        return {
            "metrics_account": "acct_after",                  # Char
            "metrics_retention_days": 31,                     # Integer
            "metrics_central_host_id": self.host.id,          # Many2one
            "metrics_delete_auth_key": "key-after",           # EncryptedChar
        }

    def test_the_job_reads_the_new_values_without_a_query(self):
        """Each field type comes back from the job's cache, new."""
        self.executor._persist(self.settings, self._new_values())
        self.executor._persist(self.inst, {"environment": "production"})
        with self.assertQueryCount(0, flush=False):
            seen = (
                self.settings.metrics_account,
                self.settings.metrics_retention_days,
                self.settings.metrics_central_host_id.id,
                self.settings.metrics_delete_auth_key,
                self.inst.environment,                        # Selection
            )
        self.assertEqual(seen, (
            "acct_after", 31, self.host.id, "key-after", "production",
        ))

    def test_the_values_survive_the_end_of_the_job(self):
        """
        The written values are in the database after the runner's clear().
        """
        new = self._new_values()
        self.executor._persist(self.settings, new)
        self.end_job()
        for fname, value in new.items():
            self.assertEqual(self.stored(self.settings, fname), value, fname)

    def test_a_plain_write_from_the_job_is_lost(self):
        """The failure the helper exists for, pinned as it happens."""
        self.settings.write({"metrics_retention_days": 99})
        self.end_job()
        self.assertEqual(self.stored(self.settings, "metrics_retention_days"), 30)

    def test_leaves_nothing_for_the_job_to_flush(self):
        """A second write of the same row from the job would be the deadlock."""
        self.executor._persist(self.settings, self._new_values())
        with self.assertQueryCount(0, flush=False):
            self.env.flush_all()

    def test_carries_what_the_write_recomputed(self):
        """
        A stored computed field of the record follows the write into the cache.
        """
        before = self.inst.rebuild_fingerprint
        self.executor._persist(self.inst, {"pip_dependencies": "requests==3"})
        with self.assertQueryCount(0, flush=False):
            seen = self.inst.rebuild_fingerprint
        self.end_job()
        self.assertNotEqual(seen, before)
        self.assertEqual(seen, self.stored(self.inst, "rebuild_fingerprint"))

    def test_writes_with_the_records_own_context(self):
        """``record.with_context(...)`` must reach the write, not the job's env.

        ``pip_provenance_managed`` is what keeps a managed rewrite of
        ``pip_dependencies`` from pruning the provenance map.
        """
        project = self.env["cloud.project"].create({"name": "Provenance"})
        inst = self.env["cloud.instance"].create({
            "name": "provinst", "project_id": project.id,
            "environment": "staging", "pip_dependencies": "requests==2",
            "pip_dependency_sources": {"requests": {"spec": "requests==2"}},
        })
        self.env.flush_all()
        self.executor._persist(
            inst.with_context(pip_provenance_managed=True),
            {"pip_dependencies": "requests==3"},
        )
        self.end_job()
        self.assertIn("requests", self.stored(inst, "pip_dependency_sources"))

        self.executor._persist(inst, {"pip_dependencies": "requests==4"})
        self.end_job()
        # A pruned map is empty, which the Json column stores as NULL.
        self.assertNotIn(
            "requests", self.stored(inst, "pip_dependency_sources") or {},
        )

    def test_refuses_x2many_commands(self):
        """x2many commands have no cache form, so they are refused."""
        with self.assertRaises(ValueError):
            self.executor._persist(self.inst, {"repo_ids": [(5, 0, 0)]})
