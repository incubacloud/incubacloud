"""The guard against a repeated job, on real cursors.

Outside registry test mode ``registry.cursor()`` is a connection of its
own: the hooks' durable cursor commits before the runner's transaction,
whose REPEATABLE READ snapshot does not see it — what production does,
and what ``test_job_outcome`` cannot reproduce. Here the fixtures are
committed, a "runner" cursor takes its snapshot as queue_job's
``job.lock()`` does, the job runs through ``cloud.job.execute``, another
cursor stamps the instance as the metrics cron does, and the store of
``done`` either commits or meets a real ``SerializationFailure``.

Everything committed is removed at the end (``_remove_fixtures``).
"""
import contextlib
from unittest.mock import MagicMock, patch

from psycopg2 import errorcodes
from psycopg2.errors import SerializationFailure

from odoo import SUPERUSER_ID, api
from odoo.tests.common import TransactionCase

from ..models import abstract_executor
from ..models.cloud_host import CloudHost
from ..models.delete_instance_executor import DeleteInstanceExecutor
from ..models.transport import CommandResult, SSHTransport
from .test_job_outcome import _OWN, _OutcomeProbe


class _RealCursorCase(TransactionCase):
    """Committed fixtures: a deployed instance and its dispatched job."""

    #: The job type of the dispatched job.
    job_code = "restart_instance"

    def setUp(self):
        """Commit the fixtures on a cursor of their own; remove them at the end."""
        super().setUp()
        self.addCleanup(self._remove_fixtures)
        self.ids = {}
        slug = self.job_code.replace("_", "-")
        with self._cursor() as env:
            host = env["cloud.host"].create({
                "name": f"real-cursor-{self.job_code}",
                "ip_address": "192.0.2.95",
                "user": "ubuntu",
                "wildcard_domain": f"real-{slug}.example.com",
            })
            project = env["cloud.project"].create({"name": f"Real {self.job_code}"})
            inst = env["cloud.instance"].create({
                "name": f"real-{slug}",
                "project_id": project.id,
                "environment": "production",
                "host_id": host.id,
            })
            inst._transition("deploying")
            inst._transition("deployed")
            job_type = env["cloud.job.type"].search(
                [("code", "=", self.job_code)], limit=1,
            )
            qjob = env["queue.job"].create({
                "uuid": f"real-cursor-{self.job_code}-{inst.id}",
                "name": self.job_code,
                "state": "started",
                "method_name": "execute",
                "model_name": "cloud.job",
                "func_string": "execute()",
            })
            job = env["cloud.job"].create({
                "host_id": host.id,
                "instance_id": inst.id,
                "job_type_id": job_type.id,
                "name": self.job_code,
                "queue_job_uuid": qjob.uuid,
                "queue_job_id": qjob.id,
            })
            self.ids = {
                "host": host.id, "project": project.id, "instance": inst.id,
                "job": job.id, "queue_job": qjob.id, "uuid": qjob.uuid,
            }
        self.transport = MagicMock(spec=SSHTransport)
        self.transport.execute.return_value = CommandResult(stdout="", exit_status=0)
        self.transport.run.return_value = CommandResult(stdout="", exit_status=0)

    @contextlib.contextmanager
    def _cursor(self):
        """A real cursor of its own, committed when the block ends."""
        with self.registry.cursor() as cr:
            yield api.Environment(cr, SUPERUSER_ID, {})

    def _remove_fixtures(self):
        """Delete, by SQL, everything the test committed."""
        if not self.ids:
            return
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT id FROM cloud_job WHERE host_id = %s", (self.ids["host"],),
            )
            jobs = tuple(row[0] for row in cr.fetchall()) or (0,)
            cr.execute(
                "SELECT queue_job_uuid FROM cloud_job WHERE id IN %s"
                " AND queue_job_uuid IS NOT NULL", (jobs,),
            )
            uuids = tuple(row[0] for row in cr.fetchall()) + (self.ids["uuid"],)
            for query, params in (
                ("DELETE FROM cloud_job_outcome WHERE job_id IN %s", (jobs,)),
                ("DELETE FROM cloud_job_log_chunk WHERE job_id IN %s", (jobs,)),
                ("DELETE FROM cloud_alert WHERE host_id = %s OR instance_id = %s"
                 " OR job_id IN %s", (self.ids["host"], self.ids["instance"], jobs)),
                ("DELETE FROM cloud_job WHERE id IN %s", (jobs,)),
                ("DELETE FROM queue_job WHERE uuid IN %s", (uuids,)),
                ("DELETE FROM cloud_instance WHERE id = %s", (self.ids["instance"],)),
                ("DELETE FROM cloud_project WHERE id = %s", (self.ids["project"],)),
                ("DELETE FROM cloud_host WHERE id = %s", (self.ids["host"],)),
            ):
                cr.execute(query, params)

    @contextlib.contextmanager
    def _outside(self, executor_cls):
        """Patch the host, the job's executor and what reaches outside.

        :return: ``(get_transport, notified)`` mocks
        """
        transport = self.transport

        @contextlib.asynccontextmanager
        async def connect(_host):
            yield transport

        Job = type(self.env["cloud.job"])
        with patch.object(
            Job, "_get_executor", autospec=True, return_value=executor_cls,
        ), patch.object(
            CloudHost, "get_transport", autospec=True, side_effect=connect,
        ) as connected, patch.object(
            executor_cls, "_refresh_host_disk", autospec=True,
        ), patch.object(
            Job, "_notify_by_email", autospec=True,
        ) as notified, patch.object(Job, "_notify_external", autospec=True), \
                patch.object(
                    type(self.env["cloud.host"]), "refresh_observability_labels",
                    autospec=True,
                ):
            yield connected, notified

    @contextlib.contextmanager
    def _runner(self):
        """The runner's cursor, its snapshot taken as ``job.lock()`` takes it."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            cr.execute(
                "SELECT id FROM queue_job WHERE uuid = %s FOR UPDATE",
                (self.ids["uuid"],),
            )
            yield env

    def _attempt(self, env):
        """``execute`` in the savepoint queue_job runs it in; return its result."""
        job = env["cloud.job"].browse(self.ids["job"])
        with env.cr.savepoint():
            return job.execute()

    def _store_done(self, env, result=None):
        """Store the job done, as ``RunJobController`` does."""
        env["queue.job"].browse(self.ids["queue_job"]).write({
            "state": "done", "result": result,
        })
        env.flush_all()

    def _stamp_the_instance(self):
        """What the metrics cron does meanwhile: update the instance's row."""
        with self.registry.cursor() as cr:
            cr.execute(
                "UPDATE cloud_instance SET write_date = now() at time zone 'UTC'"
                " WHERE id = %s", (self.ids["instance"],),
            )

    def _meet_the_instance(self, env):
        """A write of the store's that meets the instance's row, as before C1.

        Until C1 the store of ``done`` queued the instance's state probe,
        a ``cloud_job`` row whose foreign key locks the instance's row
        ``FOR KEY SHARE`` (the ``CONTEXT`` of the 7-nov failures). Here
        that lock alone did not fail — the run had taken one on the row
        before its hook, it seems, and PostgreSQL does not check again a
        lock the transaction holds —, so the row is updated instead,
        which PostgreSQL refuses on a row changed after the snapshot.
        """
        env.cr.execute(
            "UPDATE cloud_instance SET write_date = now() at time zone 'UTC'"
            " WHERE id = %s",
            (self.ids["instance"],), log_exceptions=False,
        )

    def _start_again(self):
        """queue_job hands the job to a worker again: ``started``, committed."""
        with self._cursor() as env:
            env["queue.job"].browse(self.ids["queue_job"]).write({"state": "started"})

    def _lose_a_serialization_race(self, executor_cls):
        """First attempt: the hook commits; the store meets a real race, rolls back."""
        with self._outside(executor_cls), self._runner() as env:
            self._attempt(env)
            self._stamp_the_instance()
            self._store_done(env)
            with self.assertRaises(SerializationFailure) as raised:
                self._meet_the_instance(env)
            self.assertEqual(
                raised.exception.pgcode, errorcodes.SERIALIZATION_FAILURE,
            )
            env.cr.rollback()
        self._start_again()

    def _row(self, table, record_id, columns):
        """*columns* of a row as committed, or None."""
        with self.registry.cursor() as cr:
            cr.execute(
                f'SELECT {", ".join(columns)} FROM "{table}" WHERE id = %s',
                (record_id,),
            )
            return cr.fetchone()


class TestTheGuardOnRealCursors(_RealCursorCase):
    """A success committed, a store lost: the next attempt only finishes it."""

    def test_the_store_of_done_meets_nothing_of_the_hooks(self):
        """With C1 the store keeps to the job's rows: it commits after a real hook."""
        with self._outside(_OutcomeProbe) as (_connected, notified), \
                self._runner() as env:
            self._attempt(env)
            self._stamp_the_instance()
            self._store_done(env)
            notified.assert_not_called()
            env.cr.commit()
            notified.assert_called_once()
        self.assertEqual(
            self._row("queue_job", self.ids["queue_job"], ["state"]), ("done",),
        )
        self.assertIsNone(self._row("cloud_instance", self.ids["instance"], ["id"]))

    def test_the_repetition_ends_at_the_mark(self):
        """No connection; the own row and the owed unlink come back; one mark."""
        self._lose_a_serialization_race(_OutcomeProbe)
        self.assertIsNotNone(self._row("cloud_instance", self.ids["instance"], ["id"]))
        with self._outside(_OutcomeProbe) as (connected, notified), \
                self._runner() as env:
            self.assertEqual(self._attempt(env), abstract_executor.ALREADY_DONE)
            connected.assert_not_called()
            self._store_done(env, abstract_executor.ALREADY_DONE)
            env.cr.commit()
            notified.assert_called_once()
        self.assertEqual(
            self._row("cloud_job", self.ids["job"], ["state"]), ("done",),
        )
        with self._cursor() as env:
            job = env["cloud.job"].browse(self.ids["job"])
            self.assertEqual(job.result, _OWN["result"])
            env.cr.execute(
                "SELECT count(*) FROM cloud_job_outcome WHERE job_id = %s",
                (self.ids["job"],),
            )
            self.assertEqual(env.cr.fetchone()[0], 1)
        self.assertIsNone(self._row("cloud_instance", self.ids["instance"], ["id"]))


class TestADeleteOnRealCursors(_RealCursorCase):
    """``delete_instance``: the unlink waits for the commit, also after a lost one."""

    job_code = "delete_instance"

    def test_the_instance_goes_after_the_commit(self):
        """The hook marks it, the store commits, the unlink follows on a cursor."""
        with self._outside(DeleteInstanceExecutor), self._runner() as env:
            self._attempt(env)
            self._stamp_the_instance()
            self._store_done(env)
            self.assertIsNotNone(
                self._row("cloud_instance", self.ids["instance"], ["id"]),
            )
            env.cr.commit()
        self.assertIsNone(self._row("cloud_instance", self.ids["instance"], ["id"]))
        self.assertEqual(self._row("cloud_job", self.ids["job"], ["state"]), ("done",))

    def test_after_a_lost_race_the_unlink_is_armed_again(self):
        """The repetition reaches no host and removes the record it owes."""
        self._lose_a_serialization_race(DeleteInstanceExecutor)
        self.assertEqual(
            self._row("cloud_instance", self.ids["instance"], ["removal_finalized"]),
            (True,),
        )
        with self._outside(DeleteInstanceExecutor) as (connected, _notified), \
                self._runner() as env:
            self.assertEqual(self._attempt(env), abstract_executor.ALREADY_DONE)
            connected.assert_not_called()
            self._store_done(env, abstract_executor.ALREADY_DONE)
            env.cr.commit()
        self.assertIsNone(self._row("cloud_instance", self.ids["instance"], ["id"]))
