"""The probe of running panels leaves a job's store of ``done`` alone.

The cron that reads which panels run (``_cron_update_running_from_metrics``)
asks ``_running_confirmed_after`` or ``_running_stopped_since`` with the
instance's row locked, writes the instance, and commits on a READ
COMMITTED cursor of its own. A job keyed on that instance runs in a
REPEATABLE READ transaction whose snapshot can be older than that
commit. Held ``FOR UPDATE``, the probe's write counted as a change of the
instance's key, and the job's insert or update of a row keyed on it
failed to serialize: a move failed so on 7-nov-2026. Held ``FOR NO KEY
UPDATE``, the probe excludes the same writers and the key check passes.

The transactions here are real, as in ``test_move_own_job_row``: a
fixture is committed (and removed after), the job's transaction takes its
snapshot, the probe runs and commits on its own cursor, and then the
job's transaction stores a job keyed on the instance.
"""
from datetime import timedelta

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api, fields
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from ..models._concurrency import read_committed_cursor


class TestRunningProbeUnderRealTransactions(TransactionCase):
    """The probe and a job's transaction, run by real cursors."""

    def setUp(self):
        """Commit a host, a running instance and a job type; remove them after."""
        super().setUp()
        self.registry = self.env.registry
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            host = env["cloud.host"].create({
                "name": "soft-lock-host", "ip_address": "192.0.2.93", "port": 22,
                "user": "root", "login_type": "ssh_key",
                "wildcard_domain": "soft-lock-host.example.com",
                "status": "compatible",
            })
            project = env["cloud.project"].create({"name": "soft-lock-proj"})
            inst = env["cloud.instance"].create({
                "name": "soft-lock-inst", "project_id": project.id,
                "environment": "staging", "host_id": host.id,
                "state": "deployed",
            })
            inst.write({"running": True})
            self.job_type_id = env["cloud.job.type"].search(
                [("code", "=", "host_probe")], limit=1,
            ).id
            self.host_id, self.project_id, self.instance_id = (
                host.id, project.id, inst.id,
            )
            cr.commit()
        self.addCleanup(self._remove_fixture)

    def _remove_fixture(self):
        """Delete the committed rows, children first."""
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM cloud_job WHERE host_id = %s OR instance_id = %s",
                (self.host_id, self.instance_id),
            )
            job_ids = tuple(r[0] for r in cr.fetchall()) or (0,)
            for table in ("cloud_job_log_chunk", "cloud_audit_log", "cloud_alert"):
                cr.execute(f"DELETE FROM {table} WHERE job_id IN %s", (job_ids,))
            cr.execute("DELETE FROM cloud_job WHERE id IN %s", (job_ids,))
            for table in ("cloud_alert", "cloud_audit_log"):
                cr.execute(
                    f"DELETE FROM {table} WHERE instance_id = %s", (self.instance_id,),
                )
            cr.execute("DELETE FROM cloud_instance WHERE id = %s", (self.instance_id,))
            cr.execute("DELETE FROM cloud_project WHERE id = %s", (self.project_id,))
            for table in ("cloud_alert", "cloud_audit_log", "cloud_host_whitelist"):
                cr.execute(f"DELETE FROM {table} WHERE host_id = %s", (self.host_id,))
            cr.execute("DELETE FROM cloud_host WHERE id = %s", (self.host_id,))
            cr.commit()

    def _probe(self, ask):
        """Run the probe on the instance as the cron does, and commit.

        On a READ COMMITTED cursor of its own: the question with the row
        locked, then the cron's write of the instance.

        :param ask: ``callable(instance)`` asking with the row locked
        """
        with read_committed_cursor(self.registry) as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            inst = env["cloud.instance"].browse(self.instance_id)
            ask(inst)
            inst.write({"metrics_last_seen": fields.Datetime.now()})

    def _job_transaction_around(self, ask):
        """Take a job's snapshot, run the probe, then store a job on the instance.

        :param ask: see :meth:`_probe`
        :raises SerializationFailure: when the store conflicts
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            # The job's first read fixes its snapshot, as ``job.lock()`` does.
            cr.execute(
                "SELECT state FROM cloud_instance WHERE id = %s", (self.instance_id,),
            )
            self._probe(ask)
            env["cloud.job"].create({
                "name": "soft-lock-job", "host_id": self.host_id,
                "instance_id": self.instance_id, "job_type_id": self.job_type_id,
            })
            env.flush_all()
            cr.commit()

    def _stored_jobs(self):
        """Return how many jobs keyed on the instance are committed."""
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT count(*) FROM cloud_job WHERE instance_id = %s",
                (self.instance_id,),
            )
            return cr.fetchone()[0]

    def test_the_harness_meets_the_conflict_a_strong_lock_makes(self):
        """What the probe did before: the row held FOR UPDATE, then written."""

        def ask_for_update(inst):
            """Lock the row as the probe locked it until 7-nov-2026."""
            inst.env.cr.execute(
                "SELECT running_confirmed_at FROM cloud_instance"
                " WHERE id = %s FOR UPDATE",
                (inst.id,),
            )

        with mute_logger("odoo.sql_db"), self.assertRaises(SerializationFailure):
            self._job_transaction_around(ask_for_update)
        self.assertEqual(self._stored_jobs(), 0)

    def test_a_job_is_stored_after_the_confirmation_check(self):
        """``_running_confirmed_after`` leaves the job's transaction alone."""
        moment = fields.Datetime.now() - timedelta(minutes=5)
        self._job_transaction_around(lambda inst: inst._running_confirmed_after(moment))
        self.assertEqual(self._stored_jobs(), 1)

    def test_a_job_is_stored_after_the_stop_check(self):
        """``_running_stopped_since`` leaves the job's transaction alone."""
        moment = fields.Datetime.now() - timedelta(minutes=5)
        self._job_transaction_around(lambda inst: inst._running_stopped_since(moment))
        self.assertEqual(self._stored_jobs(), 1)

    def test_the_lock_still_excludes_a_writer(self):
        """The soft lock keeps its purpose: a second writer finds the row held."""
        moment = fields.Datetime.now() - timedelta(minutes=5)
        with read_committed_cursor(self.registry) as holder:
            env = api.Environment(holder, SUPERUSER_ID, {})
            env["cloud.instance"].browse(self.instance_id)._running_confirmed_after(
                moment,
            )
            with self.registry.cursor() as other:
                other.execute(
                    "SELECT id FROM cloud_instance WHERE id = %s"
                    " FOR NO KEY UPDATE SKIP LOCKED",
                    (self.instance_id,),
                )
                self.assertIsNone(other.fetchone())
