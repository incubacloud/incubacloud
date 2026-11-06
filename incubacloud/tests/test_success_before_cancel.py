"""A run whose ``on_success`` returned before a stop ends ``done``.

The stop is not read once the outcome hook started, but ``cancel_job``
had already committed the flag on the job's ``cloud_job`` row, in a
transaction of its own. queue_job stores the job ``done`` in the job's
REPEATABLE READ transaction, whose snapshot predates the flag: the mirror
of that state on the row fails to serialize, queue_job puts the job back
in the queue from a cursor of its own (``retry_postpone``), and
``queue_job_ext`` read the flag there and stored it cancelled. Its chain
ended with it: a cutover that had cut over cancelled the freeze of its
source (6-nov-2026).

The transactions here are real, as in ``test_move_own_job_row``: a
fixture is committed (and removed after), the job's transaction takes its
snapshot, a person's cancel commits on another cursor, the outcome is
dispatched as ``AbstractExecutor.run`` dispatches it, and the job is
stored as queue_job's runner stores it.

A delete's and a purge's ``on_success`` leave the unlink of their instance
for the job's commit, which the conflict rolls back: the job ended done
with its instance still in the panel. The cursor that stores it done
arms that unlink again.
"""
import asyncio
import contextlib
from unittest.mock import patch

from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.queue_job.job import Job

from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.host_probe_executor import HostProbeExecutor
from ..models.purge_archived_executor import PurgeArchivedBackupsExecutor


class TestSuccessBeforeCancelUnderRealTransactions(TransactionCase):
    """A probe and the step that waits on it, run by real cursors."""

    #: the executor whose outcome is dispatched, and its job type
    _executor_cls = HostProbeExecutor
    _job_type_code = "host_probe"

    def setUp(self):
        """Commit a host, a started job and the step of its chain after it."""
        super().setUp()
        self.registry = self.env.registry
        self.instance_id = self.project_id = None
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            host = env["cloud.host"].create({
                "name": "before-cancel-host", "ip_address": "192.0.2.92",
                "port": 22, "user": "root", "login_type": "ssh_key",
                "wildcard_domain": "before-cancel-host.example.com",
                "status": "compatible",
            })
            job_type = env["cloud.job.type"].search(
                [("code", "=", self._job_type_code)], limit=1,
            )
            extra = self._head_vals(env, host)
            head, tail = env["cloud.job"].create([
                {"name": name, "host_id": host.id, "job_type_id": job_type.id}
                | (extra if name.endswith("head") else {})
                for name in ("before-cancel-head", "before-cancel-tail")
            ])
            head_job, tail_job = Job(head.execute), Job(tail.execute)
            tail_job.add_depends({head_job})
            head_job.graph_uuid = tail_job.graph_uuid = "graph-before-cancel"
            head_job.state = "started"
            head_job.store()
            tail_job.store()
            head.write({"queue_job_uuid": head_job.uuid})
            tail.write({"queue_job_uuid": tail_job.uuid})
            self.host_id = host.id
            self.job_ids = (head.id, tail.id)
            self.uuids = (head_job.uuid, tail_job.uuid)
            cr.commit()
        self.addCleanup(self._remove_fixture)

    def _head_vals(self, env, host):
        """What the head's job carries besides its name, host and type.

        :param env: the fixture's environment
        :param host: the fixture's ``cloud.host``
        :rtype: dict
        """
        return {}

    def _outcome_patches(self):
        """The patches the head's outcome runs under: a hook doing nothing.

        :rtype: list
        """
        return [patch.object(self._executor_cls, "on_success", autospec=True)]

    def _remove_fixture(self):
        """Delete the committed rows, children first."""
        with self.registry.cursor() as cr:
            ids, uuids = self.job_ids, self.uuids
            cr.execute(
                "DELETE FROM queue_job_lock WHERE queue_job_id IN"
                " (SELECT id FROM queue_job WHERE uuid IN %s)", (uuids,),
            )
            for table in ("cloud_job_log_chunk", "cloud_audit_log", "cloud_alert"):
                cr.execute(f"DELETE FROM {table} WHERE job_id IN %s", (ids,))
            cr.execute("DELETE FROM cloud_job WHERE id IN %s", (ids,))
            cr.execute("DELETE FROM queue_job WHERE uuid IN %s", (uuids,))
            for table in ("cloud_alert", "cloud_audit_log", "cloud_host_whitelist"):
                cr.execute(
                    f"DELETE FROM {table} WHERE host_id = %s", (self.host_id,),
                )
            if self.instance_id:
                cr.execute(
                    "DELETE FROM cloud_instance WHERE id = %s", (self.instance_id,),
                )
                cr.execute(
                    "DELETE FROM cloud_project WHERE id = %s", (self.project_id,),
                )
            cr.execute("DELETE FROM cloud_host WHERE id = %s", (self.host_id,))
            cr.commit()

    def _cancel(self):
        """A person's cancel of the head, committed on a cursor of its own."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            env["cloud.job"].browse(self.job_ids[0]).cancel_job()
            cr.commit()

    def _run_and_store_done(self, cancel_during_run):
        """Run the head's outcome well, then store it done as the runner does.

        :param bool cancel_during_run: commit a cancel after the job's
            snapshot and before its success hook
        :raises SerializationFailure: when the store of ``done`` conflicts
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            job = env["cloud.job"].browse(self.job_ids[0])
            # The snapshot, as ``job.lock()`` takes it.
            cr.execute("SELECT state FROM cloud_job WHERE id = %s", (job.id,))
            if cancel_during_run:
                self._cancel()
            executor = self._executor_cls(job, job.host_id)
            with patch.object(
                self._executor_cls, "parse_results", autospec=True, return_value=[],
            ), patch.object(
                self._executor_cls, "_refresh_host_disk", autospec=True,
            ), contextlib.ExitStack() as stack:
                for patcher in self._outcome_patches():
                    stack.enter_context(patcher)
                asyncio.run(executor._dispatch_outcome({}))
            # ``cloud.job.execute`` clears the environment; the runner's
            # ``_try_perform_job`` stores the end.
            env.clear()
            stored = Job.load(env, self.uuids[0])
            stored.set_done()
            stored.store()
            env.flush_all()
            cr.commit()

    def _postpone(self, message):
        """What queue_job's ``retry_postpone`` stores, on a cursor of its own.

        :param str message: the error the store of the end met
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            stored = Job.load(env, self.uuids[0])
            stored.postpone(result=message, seconds=5)
            stored.set_pending(reset_retry=False)
            stored.store()
            cr.commit()

    def _states(self):
        """``(head cloud job, head queue job, tail queue job)`` states."""
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT state FROM cloud_job WHERE id = %s", (self.job_ids[0],),
            )
            head = cr.fetchone()[0]
            cr.execute(
                "SELECT uuid, state FROM queue_job WHERE uuid IN %s", (self.uuids,),
            )
            queue = dict(cr.fetchall())
        return head, queue[self.uuids[0]], queue[self.uuids[1]]

    def test_a_success_the_cancel_reached_late_ends_done_with_its_chain(self):
        """The store meets the conflict, the postpone stores ``done``, and
        the step after it is set going instead of cancelled."""
        with mute_logger("odoo.sql_db"), self.assertRaises(SerializationFailure) as caught:
            self._run_and_store_done(cancel_during_run=True)
        self._postpone(str(caught.exception))
        head, head_queue, tail_queue = self._states()
        self.assertEqual(head_queue, "done")
        self.assertEqual(head, "done")
        self.assertEqual(tail_queue, "pending")

    def test_a_success_nobody_cancelled_writes_nothing_aside(self):
        """No mark on a job nobody cancelled: its store meets no conflict."""
        self._run_and_store_done(cancel_during_run=False)
        head, head_queue, _tail = self._states()
        self.assertEqual((head, head_queue), ("done", "done"))
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT succeeded_before_cancel FROM cloud_job WHERE id = %s",
                (self.job_ids[0],),
            )
            self.assertFalse(cr.fetchone()[0])


class TestDeleteSucceededBeforeCancelUnderRealTransactions(
    TestSuccessBeforeCancelUnderRealTransactions,
):
    """A delete of an instance and the step after it, run by real cursors."""

    _executor_cls = DeleteInstanceExecutor
    _job_type_code = "delete_instance"

    def _head_vals(self, env, host):
        """Commit an instance on *host* for the head to delete.

        :param env: the fixture's environment
        :param host: the fixture's ``cloud.host``
        :return: the head's ``instance_id``
        :rtype: dict
        """
        project = env["cloud.project"].create({"name": "before-cancel-proj"})
        inst = env["cloud.instance"].create(self._instance_vals(project, host))
        self.project_id, self.instance_id = project.id, inst.id
        return {"instance_id": inst.id}

    def _instance_vals(self, project, host):
        """The fields of the instance the head removes.

        :rtype: dict
        """
        return {
            "name": "before-cancel-inst", "project_id": project.id,
            "environment": "staging", "host_id": host.id, "state": "deployed",
        }

    def _outcome_patches(self):
        """The executor's own hook, with the host's labels left alone.

        :rtype: list
        """
        return [patch.object(
            type(self.env["cloud.host"]), "refresh_observability_labels",
            autospec=True,
        )]

    def _instance_exists(self):
        """Whether the instance's row is still committed.

        :rtype: bool
        """
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT 1 FROM cloud_instance WHERE id = %s", (self.instance_id,),
            )
            return bool(cr.fetchone())

    def _assert_late_cancel_unlinks(self):
        """The conflict rolls back the unlink ``on_success`` left for the
        job's commit; the postpone that stores ``done`` arms it again."""
        with mute_logger("odoo.sql_db"), self.assertRaises(SerializationFailure) as caught:
            self._run_and_store_done(cancel_during_run=True)
        self.assertTrue(self._instance_exists())
        self._postpone(str(caught.exception))
        head, head_queue, tail_queue = self._states()
        self.assertEqual((head, head_queue, tail_queue), ("done", "done", "pending"))
        self.assertFalse(self._instance_exists())

    def _assert_no_cancel_unlinks(self):
        """The unlink fires on the commit of the job's own transaction."""
        self._run_and_store_done(cancel_during_run=False)
        self.assertEqual(self._states()[:2], ("done", "done"))
        self.assertFalse(self._instance_exists())

    def test_a_removal_the_cancel_reached_late_unlinks_its_instance(self):
        """See ``_assert_late_cancel_unlinks``."""
        self._assert_late_cancel_unlinks()

    def test_a_removal_nobody_cancelled_unlinks_its_instance(self):
        """See ``_assert_no_cancel_unlinks``."""
        self._assert_no_cancel_unlinks()


class TestPurgeSucceededBeforeCancelUnderRealTransactions(
    TestDeleteSucceededBeforeCancelUnderRealTransactions,
):
    """A purge of an archived instance and the step after it."""

    _executor_cls = PurgeArchivedBackupsExecutor
    _job_type_code = "purge_archived_backups"

    def _instance_vals(self, project, host):
        """An archived instance: back in 'draft', out of the lists.

        :rtype: dict
        """
        return super()._instance_vals(project, host) | {
            "state": "draft", "active": False,
        }

    def _outcome_patches(self):
        """The executor's own hook.

        :rtype: list
        """
        return []

    def test_a_purge_the_cancel_reached_late_unlinks_its_instance(self):
        """See ``_assert_late_cancel_unlinks``."""
        self._assert_late_cancel_unlinks()

    def test_a_purge_nobody_cancelled_unlinks_its_instance(self):
        """See ``_assert_no_cancel_unlinks``."""
        self._assert_no_cancel_unlinks()
